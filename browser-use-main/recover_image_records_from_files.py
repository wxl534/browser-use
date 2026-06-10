"""
Recover missing image_record.jsonl entries from files already present in image/.

Use this after an accidental non-resume run deleted structured records but left
valid image files in place. Existing records are preserved; only unreferenced
local images are appended with recovered metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_RECORD_FILE = BASE_DIR / 'browseruse_agent_data' / 'image_record.jsonl'
DEFAULT_IMAGE_DIR = BASE_DIR / 'image'
DEFAULT_INFO_FILE = BASE_DIR / 'browseruse_agent_data' / 'temple_photo_info.md'
DEFAULT_TITLE_FILE = BASE_DIR / 'browseruse_agent_data' / 'title.txt'
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    records: list[dict] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + '\n')


def image_files(image_dir: Path) -> list[Path]:
    if not image_dir.exists():
        return []
    return sorted(
        [
            path.resolve()
            for path in image_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ],
        key=lambda path: (sequence_from_name(path.name) or 10**9, path.stat().st_mtime, path.name.lower()),
    )


def sequence_from_name(file_name: str) -> int | None:
    stem = Path(file_name).stem
    match = re.match(r'^(?:temple|china_temple)_(\d{1,5})(?:\D|$)', stem, re.IGNORECASE)
    if not match:
        return None
    try:
        sequence = int(match.group(1))
    except ValueError:
        return None
    if sequence > 1000:
        return None
    return sequence


def record_sequence(record: dict) -> int | None:
    try:
        return int(record.get('sequence'))
    except (TypeError, ValueError):
        return None


def record_sort_key(record: dict) -> tuple[int, str]:
    sequence = record_sequence(record)
    return sequence if sequence is not None else 10**9, str(record.get('file_name') or '')


def title_from_file(path: Path, sequence: int) -> str:
    title = path.stem
    title = re.sub(r'^(?:temple|china_temple)_\d{1,5}_?', '', title, flags=re.IGNORECASE)
    title = re.sub(r'_+', '_', title).strip('_') or path.stem
    return f'china_temple_{sequence:03d}_{title}'


def markdown_cell(value: object) -> str:
    text = str(value or '').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    return re.sub(r'\s+', ' ', text.replace('|', '\\|')).strip()


def rewrite_title_and_info(records: list[dict], title_file: Path, info_file: Path) -> None:
    downloaded_records = [record for record in sorted(records, key=record_sort_key) if record.get('status') == 'downloaded']
    titles = [
        str(record.get('title') or record.get('collection_title') or record.get('file_name') or 'untitled').strip()
        for record in downloaded_records
    ]
    title_file.parent.mkdir(parents=True, exist_ok=True)
    title_file.write_text('\n'.join([*titles, 'END', '']), encoding='utf-8')

    lines = [
        '# 图片下载记录',
        '',
        '| 序号 | 保存文件名 | 重命名标题 | 藏品标题 | 藏品 URL | 图片 URL | 相关证据 | 作者/时代/分类/馆藏号 | 简短说明 |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    for record in downloaded_records:
        lines.append(
            '| '
            + ' | '.join(
                [
                    markdown_cell(record.get('sequence')),
                    markdown_cell(record.get('file_name')),
                    markdown_cell(record.get('title')),
                    markdown_cell(record.get('collection_title')),
                    markdown_cell(record.get('page_url')),
                    markdown_cell(record.get('image_url')),
                    markdown_cell(record.get('evidence')),
                    markdown_cell(record.get('metadata')),
                    markdown_cell(record.get('summary')),
                ]
            )
            + ' |'
        )
    info_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_path = path.with_suffix(path.suffix + f'.bak_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
    shutil.copy2(path, backup_path)
    return backup_path


def recover_records(record_file: Path, image_dir: Path, title_file: Path, info_file: Path, apply: bool) -> dict:
    records = load_jsonl(record_file)
    existing_hashes = {
        str(record.get('sha256') or '').strip().lower()
        for record in records
        if record.get('status') == 'downloaded' and record.get('sha256')
    }
    existing_files = {
        Path(str(record.get('file_name') or '')).name
        for record in records
        if record.get('status') == 'downloaded' and record.get('file_name')
    }
    used_sequences = {
        sequence
        for sequence in (record_sequence(record) for record in records if record.get('status') == 'downloaded')
        if sequence is not None
    }
    next_sequence = max(used_sequences or {0}) + 1

    recovered: list[dict] = []
    skipped_existing = 0
    now = utc_now()
    for image_path in image_files(image_dir):
        file_hash = sha256_file(image_path)
        if file_hash in existing_hashes or image_path.name in existing_files:
            skipped_existing += 1
            continue

        sequence = sequence_from_name(image_path.name)
        if sequence is None or sequence in used_sequences:
            sequence = next_sequence
            next_sequence += 1
        used_sequences.add(sequence)
        next_sequence = max(next_sequence, sequence + 1)

        title = title_from_file(image_path, sequence)
        recovered.append(
            {
                'status': 'downloaded',
                'sequence': sequence,
                'file_name': image_path.name,
                'file_path': str(image_path),
                'file_size': image_path.stat().st_size,
                'sha256': file_hash,
                'title': title,
                'collection_title': image_path.stem,
                'page_url': '',
                'image_url': '',
                'evidence': 'Recovered from local image folder after structured record loss.',
                'metadata': 'Recovered local file; original page metadata unavailable.',
                'summary': 'Recovered local image file.',
                'recorded_at': now,
                'recovered_from_file': True,
            }
        )

    merged_records = sorted([*records, *recovered], key=record_sort_key)
    backups: list[str] = []
    if apply and recovered:
        for path in (record_file, title_file, info_file):
            backup_path = backup_file(path)
            if backup_path:
                backups.append(str(backup_path))
        write_jsonl(record_file, merged_records)
        rewrite_title_and_info(merged_records, title_file, info_file)

    return {
        'record_file': str(record_file),
        'image_dir': str(image_dir),
        'existing_records': len(records),
        'skipped_existing_images': skipped_existing,
        'recovered_records': len(recovered),
        'total_records_after_recovery': len(merged_records),
        'apply': apply,
        'backups': backups,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Recover image_record.jsonl entries from local image files.')
    parser.add_argument('--record-file', type=Path, default=DEFAULT_RECORD_FILE)
    parser.add_argument('--image-dir', type=Path, default=DEFAULT_IMAGE_DIR)
    parser.add_argument('--title-file', type=Path, default=DEFAULT_TITLE_FILE)
    parser.add_argument('--info-file', type=Path, default=DEFAULT_INFO_FILE)
    parser.add_argument('--apply', action='store_true', help='Write recovered records. Without this, only prints a summary.')
    parser.add_argument('--json', action='store_true', help='Print machine-readable JSON summary.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = recover_records(
        record_file=args.record_file.resolve(),
        image_dir=args.image_dir.resolve(),
        title_file=args.title_file.resolve(),
        info_file=args.info_file.resolve(),
        apply=args.apply,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for key, value in summary.items():
            print(f'{key}: {value}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
