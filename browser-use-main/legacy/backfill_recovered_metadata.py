"""
Backfill metadata for recovered image_record.jsonl rows when reliable evidence exists.

This script is intentionally conservative: it only updates recovered records when
logs contain an explicit sequence/file plus page_url or image_url. It does not
guess missing URLs from titles.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_RECORD_FILE = BASE_DIR / 'browseruse_agent_data' / 'image_record.jsonl'
DEFAULT_INFO_FILE = BASE_DIR / 'browseruse_agent_data' / 'temple_photo_info.md'
DEFAULT_REPORT_FILE = BASE_DIR / 'browseruse_agent_data' / 'metadata_backfill_report.json'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def log_files(base_dir: Path) -> list[Path]:
    candidates = [base_dir / 'info.log', base_dir / 'debug.log']
    history_dir = base_dir / 'history'
    if history_dir.exists():
        candidates.extend(sorted(history_dir.glob('log_backup_*/*.log')))
    return [path for path in candidates if path.exists() and path.is_file()]


def extract_log_metadata(paths: list[Path]) -> dict[int, dict]:
    by_sequence: dict[int, dict] = {}
    current_sequence: int | None = None
    current_file = ''

    sequence_patterns = [
        re.compile(r'已下载并记录图片\s*#(\d+):\s*([^,\s]+)', re.IGNORECASE),
        re.compile(r'已记录图片\s*#(\d+):\s*([^,\s]+)', re.IGNORECASE),
        re.compile(r'\bsequence:\s*(\d+)\b.*?\bfile_name:\s*([^,\s]+)', re.IGNORECASE),
    ]
    image_url_pattern = re.compile(r'(?:图片 URL|image_url):\s*(https?://\S+)', re.IGNORECASE)
    page_url_pattern = re.compile(r'(?:页面 URL|page_url):\s*(https?://\S+)', re.IGNORECASE)

    for path in paths:
        try:
            lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
        except OSError:
            continue
        for line in lines:
            for pattern in sequence_patterns:
                match = pattern.search(line)
                if match:
                    current_sequence = int(match.group(1))
                    current_file = Path(match.group(2).strip('`"\'，,;；')).name
                    item = by_sequence.setdefault(current_sequence, {'sequence': current_sequence})
                    if current_file:
                        item['file_name'] = current_file
                    break

            if current_sequence is None:
                continue

            image_match = image_url_pattern.search(line)
            if image_match:
                by_sequence.setdefault(current_sequence, {'sequence': current_sequence})['image_url'] = image_match.group(1).strip('`"\'，,;；')

            page_match = page_url_pattern.search(line)
            if page_match:
                by_sequence.setdefault(current_sequence, {'sequence': current_sequence})['page_url'] = page_match.group(1).strip('`"\'，,;；')

    return by_sequence


def markdown_cell(value: object) -> str:
    text = str(value or '').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    return re.sub(r'\s+', ' ', text.replace('|', '\\|')).strip()


def rewrite_info_file(records: list[dict], info_file: Path) -> None:
    lines = [
        '# 图片下载记录',
        '',
        '| 序号 | 保存文件名 | 重命名标题 | 藏品标题 | 藏品 URL | 图片 URL | 相关证据 | 作者/时代/分类/馆藏号 | 简短说明 |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    sorted_records = sorted(records, key=lambda record: (int(record.get('sequence') or 10**9), str(record.get('file_name') or '')))
    for record in sorted_records:
        if record.get('status') != 'downloaded':
            continue
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


def backfill(record_file: Path, info_file: Path, report_file: Path, apply: bool) -> dict:
    records = load_jsonl(record_file)
    metadata_by_sequence = extract_log_metadata(log_files(BASE_DIR))
    changed: list[dict] = []
    recovered_count = 0

    for record in records:
        if not record.get('recovered_from_file'):
            continue
        recovered_count += 1
        try:
            sequence = int(record.get('sequence'))
        except (TypeError, ValueError):
            continue
        metadata = metadata_by_sequence.get(sequence)
        if not metadata:
            continue

        updates: dict[str, str] = {}
        if not str(record.get('page_url') or '').strip() and metadata.get('page_url'):
            updates['page_url'] = metadata['page_url']
        if not str(record.get('image_url') or '').strip() and metadata.get('image_url'):
            updates['image_url'] = metadata['image_url']
        if updates:
            updates['metadata_backfilled_at'] = utc_now()
            updates['metadata_backfill_source'] = 'browser logs'
            record.update(updates)
            changed.append({'sequence': sequence, 'file_name': record.get('file_name'), 'updated_fields': sorted(updates)})

    backups: list[str] = []
    if apply and changed:
        backup_path = record_file.with_suffix(record_file.suffix + f'.bak_backfill_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
        shutil.copy2(record_file, backup_path)
        backups.append(str(backup_path))
        write_jsonl(record_file, records)
        rewrite_info_file(records, info_file)

    summary = {
        'record_file': str(record_file),
        'log_files_scanned': [str(path) for path in log_files(BASE_DIR)],
        'recovered_records': recovered_count,
        'metadata_candidates': len(metadata_by_sequence),
        'records_updated': len(changed),
        'updated_first_50': changed[:50],
        'apply': apply,
        'backups': backups,
    }
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Backfill recovered image metadata from logs.')
    parser.add_argument('--record-file', type=Path, default=DEFAULT_RECORD_FILE)
    parser.add_argument('--info-file', type=Path, default=DEFAULT_INFO_FILE)
    parser.add_argument('--report-file', type=Path, default=DEFAULT_REPORT_FILE)
    parser.add_argument('--apply', action='store_true', help='Apply updates. Without this, only writes a report.')
    parser.add_argument('--json', action='store_true', help='Print JSON summary.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = backfill(
        record_file=args.record_file.resolve(),
        info_file=args.info_file.resolve(),
        report_file=args.report_file.resolve(),
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
