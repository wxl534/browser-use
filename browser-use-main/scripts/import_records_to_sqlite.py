"""
Import downloaded image records into a local SQLite database.

The browser agent remains responsible for collection and file download. This
script is the deterministic persistence step: it reads image_record.jsonl,
validates local files, and upserts clean rows into SQLite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_RECORD_FILE = BASE_DIR / 'browseruse_agent_data' / 'image_record.jsonl'
DEFAULT_IMAGE_DIR = BASE_DIR / 'image'
DEFAULT_DB_FILE = BASE_DIR / 'browseruse_agent_data' / 'image_catalog.sqlite3'
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    errors: list[str] = []
    if not path.exists():
        return records, [f'record file not found: {path}']

    for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f'line {line_number}: invalid JSON: {exc}')
            continue
        if not isinstance(value, dict):
            errors.append(f'line {line_number}: JSON value is not an object')
            continue
        records.append(value)
    return records, errors


def extract_source_item_id(record: dict) -> str:
    for key in ('page_url', 'image_url'):
        url = str(record.get(key) or '').strip()
        parsed = urlparse(url)
        path = parsed.path.strip('/')
        parts = path.split('/')
        if parsed.hostname == 'idp.bl.uk' and len(parts) >= 2 and parts[0] == 'collection':
            return parts[1]
        if parsed.hostname == 'data.idp.bl.uk' and 'manifest' in parts:
            index = parts.index('manifest')
            if index + 1 < len(parts):
                return parts[index + 1]
        if parsed.hostname == 'data.idp.bl.uk' and 'iiif' in parts:
            try:
                index = parts.index('3')
            except ValueError:
                continue
            if index + 1 < len(parts):
                return parts[index + 1]
    return ''


def resolve_image_path(record: dict, image_dir: Path) -> Path | None:
    candidates: list[Path] = []
    for key in ('file_path', 'final_file_name', 'file_name', 'original_file_name'):
        value = str(record.get(key) or '').strip()
        if not value:
            continue
        path = Path(value)
        candidates.append(path if path.is_absolute() else image_dir / path.name)

    title = str(record.get('title') or '').strip()
    if title:
        for ext in IMAGE_EXTENSIONS:
            candidates.append(image_dir / f'{title}{ext}')

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image
    except Exception:
        return None, None

    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None, None


def connect(db_file: Path) -> sqlite3.Connection:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_file))
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=5000')
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS import_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            source_record_file TEXT NOT NULL,
            source_image_dir TEXT NOT NULL,
            status TEXT NOT NULL,
            total_records INTEGER NOT NULL DEFAULT 0,
            imported_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            error_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS images (
            id TEXT PRIMARY KEY,
            source_site TEXT NOT NULL,
            source_item_id TEXT,
            sequence INTEGER,
            status TEXT NOT NULL,
            title TEXT,
            collection_title TEXT,
            page_url TEXT,
            image_url TEXT,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            file_size INTEGER NOT NULL,
            width INTEGER,
            height INTEGER,
            evidence TEXT,
            metadata_text TEXT,
            summary TEXT,
            metadata_json TEXT NOT NULL,
            recorded_at TEXT,
            downloaded_at TEXT,
            imported_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            import_run_id TEXT NOT NULL,
            FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_images_sequence ON images(sequence);
        CREATE INDEX IF NOT EXISTS idx_images_source_item_id ON images(source_item_id);
        CREATE INDEX IF NOT EXISTS idx_images_page_url ON images(page_url);
        CREATE INDEX IF NOT EXISTS idx_images_image_url ON images(image_url);
        CREATE INDEX IF NOT EXISTS idx_images_title ON images(title);

        CREATE TABLE IF NOT EXISTS orphan_images (
            id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            file_size INTEGER NOT NULL,
            width INTEGER,
            height INTEGER,
            reason TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            import_run_id TEXT NOT NULL,
            FOREIGN KEY(import_run_id) REFERENCES import_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_orphan_images_file_name ON orphan_images(file_name);
        """
    )
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(import_runs)").fetchall()
    }
    if 'orphan_count' not in existing_columns:
        connection.execute("ALTER TABLE import_runs ADD COLUMN orphan_count INTEGER NOT NULL DEFAULT 0")


def make_image_id(sha256: str) -> str:
    return f'img_{sha256[:16]}'


def make_orphan_id(sha256: str) -> str:
    return f'orphan_{sha256[:16]}'


def image_files(image_dir: Path) -> list[Path]:
    if not image_dir.exists():
        return []
    return sorted(
        [
            path.resolve()
            for path in image_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS and path.name != 'rename_record.txt'
        ],
        key=lambda path: path.name.lower(),
    )


def import_records(record_file: Path, image_dir: Path, db_file: Path) -> dict:
    started_at = utc_now()
    run_id = str(uuid.uuid4())
    records, read_errors = read_jsonl(record_file)
    imported_count = 0
    orphan_count = 0
    skipped: list[str] = []
    errors: list[str] = [*read_errors]
    referenced_hashes: set[str] = set()
    referenced_paths: set[Path] = set()

    with connect(db_file) as connection:
        initialize_schema(connection)
        connection.execute(
            """
            INSERT INTO import_runs (
                id, started_at, source_record_file, source_image_dir, status,
                total_records, imported_count, skipped_count, error_count, error_json
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, '[]')
            """,
            (run_id, started_at, str(record_file), str(image_dir), 'running', len(records)),
        )

        for index, record in enumerate(records, start=1):
            if record.get('status') != 'downloaded':
                skipped.append(f'record {index}: status is not downloaded')
                continue

            image_path = resolve_image_path(record, image_dir)
            if image_path is None:
                errors.append(f'record {index}: image file not found for {record.get("file_name")}')
                continue

            try:
                file_hash = sha256_file(image_path)
                file_size = image_path.stat().st_size
            except OSError as exc:
                errors.append(f'record {index}: cannot read image file {image_path}: {exc}')
                continue

            recorded_hash = str(record.get('sha256') or '').strip().lower()
            if recorded_hash and recorded_hash != file_hash:
                errors.append(
                    f'record {index}: sha256 mismatch for {image_path.name}: '
                    f'record={recorded_hash}, actual={file_hash}'
                )
                continue

            referenced_hashes.add(file_hash)
            referenced_paths.add(image_path.resolve())
            connection.execute('DELETE FROM orphan_images WHERE sha256 = ?', (file_hash,))

            width, height = image_dimensions(image_path)
            sequence = record.get('sequence')
            try:
                sequence = int(sequence) if sequence is not None and str(sequence).strip() else None
            except (TypeError, ValueError):
                sequence = None

            now = utc_now()
            metadata_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
            row = {
                'id': make_image_id(file_hash),
                'source_site': 'idp.bl.uk',
                'source_item_id': extract_source_item_id(record),
                'sequence': sequence,
                'status': 'downloaded',
                'title': str(record.get('title') or '').strip(),
                'collection_title': str(record.get('collection_title') or '').strip(),
                'page_url': str(record.get('page_url') or '').strip(),
                'image_url': str(record.get('image_url') or '').strip(),
                'file_name': image_path.name,
                'file_path': str(image_path),
                'sha256': file_hash,
                'file_size': file_size,
                'width': width,
                'height': height,
                'evidence': str(record.get('evidence') or '').strip(),
                'metadata_text': str(record.get('metadata') or '').strip(),
                'summary': str(record.get('summary') or '').strip(),
                'metadata_json': metadata_json,
                'recorded_at': str(record.get('recorded_at') or '').strip(),
                'downloaded_at': str(record.get('downloaded_at') or record.get('recorded_at') or '').strip(),
                'imported_at': now,
                'updated_at': now,
                'import_run_id': run_id,
            }

            connection.execute(
                """
                INSERT INTO images (
                    id, source_site, source_item_id, sequence, status, title,
                    collection_title, page_url, image_url, file_name, file_path,
                    sha256, file_size, width, height, evidence, metadata_text,
                    summary, metadata_json, recorded_at, downloaded_at, imported_at,
                    updated_at, import_run_id
                ) VALUES (
                    :id, :source_site, :source_item_id, :sequence, :status, :title,
                    :collection_title, :page_url, :image_url, :file_name, :file_path,
                    :sha256, :file_size, :width, :height, :evidence, :metadata_text,
                    :summary, :metadata_json, :recorded_at, :downloaded_at, :imported_at,
                    :updated_at, :import_run_id
                )
                ON CONFLICT(sha256) DO UPDATE SET
                    source_site=excluded.source_site,
                    source_item_id=excluded.source_item_id,
                    sequence=excluded.sequence,
                    status=excluded.status,
                    title=excluded.title,
                    collection_title=excluded.collection_title,
                    page_url=excluded.page_url,
                    image_url=excluded.image_url,
                    file_name=excluded.file_name,
                    file_path=excluded.file_path,
                    file_size=excluded.file_size,
                    width=excluded.width,
                    height=excluded.height,
                    evidence=excluded.evidence,
                    metadata_text=excluded.metadata_text,
                    summary=excluded.summary,
                    metadata_json=excluded.metadata_json,
                    recorded_at=excluded.recorded_at,
                    downloaded_at=excluded.downloaded_at,
                    updated_at=excluded.updated_at,
                    import_run_id=excluded.import_run_id
                """,
                row,
            )
            imported_count += 1

        for image_path in image_files(image_dir):
            try:
                file_hash = sha256_file(image_path)
                file_size = image_path.stat().st_size
            except OSError as exc:
                errors.append(f'orphan scan: cannot read image file {image_path}: {exc}')
                continue

            if file_hash in referenced_hashes or image_path.resolve() in referenced_paths:
                continue

            width, height = image_dimensions(image_path)
            now = utc_now()
            row = {
                'id': make_orphan_id(file_hash),
                'file_name': image_path.name,
                'file_path': str(image_path),
                'sha256': file_hash,
                'file_size': file_size,
                'width': width,
                'height': height,
                'reason': 'file_not_referenced_by_image_record',
                'detected_at': now,
                'updated_at': now,
                'import_run_id': run_id,
            }
            connection.execute(
                """
                INSERT INTO orphan_images (
                    id, file_name, file_path, sha256, file_size, width, height,
                    reason, detected_at, updated_at, import_run_id
                ) VALUES (
                    :id, :file_name, :file_path, :sha256, :file_size, :width, :height,
                    :reason, :detected_at, :updated_at, :import_run_id
                )
                ON CONFLICT(sha256) DO UPDATE SET
                    file_name=excluded.file_name,
                    file_path=excluded.file_path,
                    file_size=excluded.file_size,
                    width=excluded.width,
                    height=excluded.height,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at,
                    import_run_id=excluded.import_run_id
                """,
                row,
            )
            orphan_count += 1

        status = 'completed' if not errors else 'completed_with_errors'
        completed_at = utc_now()
        connection.execute(
            """
            UPDATE import_runs
            SET completed_at=?, status=?, imported_count=?, skipped_count=?,
                error_count=?, orphan_count=?, error_json=?
            WHERE id=?
            """,
            (
                completed_at,
                status,
                imported_count,
                len(skipped),
                len(errors),
                orphan_count,
                json.dumps({'errors': errors, 'skipped': skipped}, ensure_ascii=False),
                run_id,
            ),
        )

    return {
        'run_id': run_id,
        'status': status,
        'record_file': str(record_file),
        'image_dir': str(image_dir),
        'db_file': str(db_file),
        'total_records': len(records),
        'imported_count': imported_count,
        'orphan_count': orphan_count,
        'skipped_count': len(skipped),
        'error_count': len(errors),
        'errors': errors[:20],
        'skipped': skipped[:20],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Import image_record.jsonl into SQLite.')
    parser.add_argument('--record-file', type=Path, default=DEFAULT_RECORD_FILE)
    parser.add_argument('--image-dir', type=Path, default=DEFAULT_IMAGE_DIR)
    parser.add_argument('--db-file', type=Path, default=DEFAULT_DB_FILE)
    parser.add_argument('--json', action='store_true', help='Print machine-readable JSON summary.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = import_records(
        record_file=args.record_file.resolve(),
        image_dir=args.image_dir.resolve(),
        db_file=args.db_file.resolve(),
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print('SQLite import completed')
        print(f"- status: {summary['status']}")
        print(f"- imported_count: {summary['imported_count']}")
        print(f"- orphan_count: {summary['orphan_count']}")
        print(f"- skipped_count: {summary['skipped_count']}")
        print(f"- error_count: {summary['error_count']}")
        print(f"- db_file: {summary['db_file']}")
        if summary['errors']:
            print('- first_errors:')
            for error in summary['errors']:
                print(f'  - {error}')
    return 0 if summary['error_count'] == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
