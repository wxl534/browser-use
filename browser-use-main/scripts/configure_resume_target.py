"""
Configure the current ImagesCache checkpoint target.

Usage:
    python configure_resume_target.py
    python configure_resume_target.py 10000

The script updates:
- task.md target_count / n value
- Images/ImagesCache/idp_progress.json
- Images/ImagesCache/run_config.json
- Images/ImagesCache/final_download_report.md
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = BASE_DIR / 'Images' / 'ImagesCache'
TASK_FILE = BASE_DIR / 'task.md'


def read_records(cache_dir: Path) -> list[dict]:
    record_file = cache_dir / 'image_record.jsonl'
    if not record_file.exists():
        return []
    records: list[dict] = []
    for line in record_file.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def image_files(cache_dir: Path) -> list[Path]:
    image_exts = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'}
    if not cache_dir.exists():
        return []
    return [path for path in cache_dir.iterdir() if path.is_file() and path.suffix.lower() in image_exts]


def sequence_values(records: list[dict]) -> list[int]:
    values: list[int] = []
    for record in records:
        try:
            values.append(int(record.get('sequence')))
        except (TypeError, ValueError):
            continue
    return values


def detect_task_target(task_text: str) -> int | None:
    match = re.search(r'\bn\s*=\s*(\d+)\b', task_text)
    if match:
        return int(match.group(1))
    match = re.search(r'target_count\s*=\s*(\d+)', task_text)
    if match:
        return int(match.group(1))
    return None


def update_task_target(task_file: Path, target_count: int) -> int | None:
    if not task_file.exists():
        return None
    text = task_file.read_text(encoding='utf-8')
    old_target = detect_task_target(text)
    if old_target is None:
        text = re.sub(r'(## 任务目标\s*)', rf'\1\n目标数量：前 n = {target_count} 张\n', text, count=1)
    else:
        # 只更新目标声明本身(`n = X` 与 `target_count=X`),不要全局替换数字字符串,
        # 否则会误伤 task.md 里的 "page 5000","limit=50" 等无关数字.
        text = re.sub(r'(\bn\s*=\s*)\d+\b', rf'\g<1>{target_count}', text)
        text = re.sub(r'(target_count\s*=\s*)\d+', rf'\g<1>{target_count}', text)
    task_file.write_text(text, encoding='utf-8')
    return old_target


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def build_report(cache_dir: Path, target_count: int, records: list[dict]) -> str:
    downloaded_records = len([record for record in records if record.get('status') == 'downloaded'])
    image_count = len(image_files(cache_dir))
    seqs = set(sequence_values(records))
    missing = [value for value in range(1, target_count + 1) if value not in seqs]
    complete = downloaded_records >= target_count and image_count >= target_count
    lines = [
        f'Final download validation: {"SUCCESS" if complete else "INCOMPLETE"}',
        f'- target_count: {target_count}',
        f'- downloaded_records: {downloaded_records}',
        f'- remaining_records_needed: {max(0, target_count - downloaded_records)}',
        f'- image_files: {image_count}',
        f'- sequence_gaps_warning_only: {missing[:200] if missing else "none"}',
        '- bad_or_empty_images: not_checked_by_configure_resume_target',
        '- duplicate_image_hash_groups: not_checked_by_configure_resume_target',
        '- orphan_files_warning_only: not_checked_by_configure_resume_target',
        f'- record_file: {cache_dir / "image_record.jsonl"}',
    ]
    return '\n'.join(lines)


def configure_target(cache_dir: Path, target_count: int, update_task: bool = True) -> dict:
    cache_dir = cache_dir.resolve()
    if not cache_dir.exists():
        raise FileNotFoundError(f'ImagesCache not found: {cache_dir}')
    records = read_records(cache_dir)
    seqs = sequence_values(records)
    downloaded_records = len([record for record in records if record.get('status') == 'downloaded'])
    next_sequence = (max(seqs) + 1) if seqs else 1

    old_target = update_task_target(TASK_FILE, target_count) if update_task else None

    progress_path = cache_dir / 'idp_progress.json'
    progress = load_json(progress_path)
    progress.update({
        'target_count': target_count,
        'downloaded_records': downloaded_records,
        'remaining_records': max(0, target_count - downloaded_records),
        'max_sequence': max(seqs) if seqs else 0,
        'next_sequence': next_sequence,
        'source': 'configured_by_configure_resume_target',
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    progress.setdefault('keyword', 'china buddhist')
    progress.setdefault('limit', 50)
    progress.setdefault('next_page', progress.get('current_page', 1))
    progress.setdefault('next_index', 0)
    write_json(progress_path, progress)

    config_path = cache_dir / 'run_config.json'
    config = load_json(config_path)
    config.update({
        'target_count': target_count,
        'run_dir': str(cache_dir),
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    config.setdefault('keyword', progress.get('keyword', 'china buddhist'))
    write_json(config_path, config)

    report = build_report(cache_dir, target_count, records)
    (cache_dir / 'final_download_report.md').write_text(report + '\n', encoding='utf-8')

    return {
        'cache_dir': str(cache_dir),
        'old_task_target': old_target,
        'new_target_count': target_count,
        'downloaded_records': downloaded_records,
        'remaining_records': max(0, target_count - downloaded_records),
        'max_sequence': max(seqs) if seqs else 0,
        'next_sequence': next_sequence,
        'progress_file': str(progress_path),
        'run_config_file': str(config_path),
        'final_report_file': str(cache_dir / 'final_download_report.md'),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Configure ImagesCache resume target.')
    parser.add_argument('target_count', nargs='?', type=int, help='目标总下载数量,例如 10000')
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument('--no-task-update', action='store_true', help='不修改 task.md')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_count = args.target_count
    if target_count is None:
        target_count = int(input('请输入新的目标总下载数量: ').strip())
    if target_count < 1:
        raise ValueError('target_count must be >= 1')
    summary = configure_target(args.cache_dir, target_count, update_task=not args.no_task_update)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
