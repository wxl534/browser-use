"""ImagesCache 目录公共工具:运行锁,内容判断,下载计数.

worker.py 与 runner.py 共用同一套 run.lock / image_record.jsonl 约定,
统一收拢以避免双方各算各的.
"""
import json
import os
from pathlib import Path


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cache_is_locked(cache_dir: Path) -> bool:
    lock_file = cache_dir / 'run.lock'
    if not lock_file.exists():
        return False
    try:
        lock = json.loads(lock_file.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return False
    try:
        pid = int(lock.get('pid') or 0)
    except (TypeError, ValueError):
        return False
    return process_is_running(pid)


def cache_has_content(cache_dir: Path) -> bool:
    return cache_dir.exists() and any(cache_dir.iterdir())


def count_downloaded_records(record_file: Path) -> int:
    """统计 image_record.jsonl 中 status=downloaded 的成功记录数量."""
    if not record_file.exists():
        return 0
    count = 0
    for line in record_file.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get('status') == 'downloaded':
            count += 1
    return count
