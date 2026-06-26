"""只读访问爬虫产物：image_catalog.sqlite3 + 运行时状态文件。

设计原则：
- 数据库可能尚未生成（还没跑过任何任务），所有查询都要优雅降级为空结果。
- 不写库；写操作（导入 SQLite）由 run_manager 在每轮结束后调用现有脚本完成。
- 统计的"已下载数"以 image_record.jsonl 为准（与 supervisor 同源），不依赖 DB 是否最新。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import paths

# images 表里允许返回给前端的列（与 import_records_to_sqlite.py 的 schema 对齐）。
IMAGE_COLUMNS = [
    'id', 'source_site', 'source_item_id', 'sequence', 'status', 'title',
    'collection_title', 'page_url', 'image_url', 'file_name', 'file_path',
    'sha256', 'file_size', 'width', 'height', 'evidence', 'metadata_text',
    'summary', 'recorded_at', 'downloaded_at', 'imported_at', 'updated_at',
    'import_run_id',
]

# 允许前端排序的列白名单，避免 SQL 注入。
SORTABLE = {'sequence', 'title', 'file_size', 'width', 'height', 'downloaded_at', 'imported_at', 'status'}


def _connect() -> sqlite3.Connection | None:
    db = paths.sqlite_file()
    if not db.exists():
        return None
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def db_available() -> bool:
    return paths.sqlite_file().exists()


def _downloaded_count_from_jsonl() -> int:
    """与 supervisor 同源的已下载计数（DB 未生成或落后时的权威来源）。"""
    record_file = paths.RECORD_FILE
    if not record_file.exists():
        return 0
    count = 0
    for line in record_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get('status') == 'downloaded':
            count += 1
    return count


def _read_target() -> int | None:
    try:
        from auto_run_until_target import read_target_from_task
        return read_target_from_task(paths.TASK_FILE)
    except Exception:
        return None


def _read_keyword() -> str:
    try:
        from auto_run_until_target import detect_search_keyword
        if paths.TASK_FILE.exists():
            return detect_search_keyword(paths.TASK_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return ''


def get_stats() -> dict[str, Any]:
    downloaded = _downloaded_count_from_jsonl()
    target = _read_target()
    stats: dict[str, Any] = {
        'downloaded': downloaded,
        'target': target,
        'keyword': _read_keyword(),
        'db_available': db_available(),
        'by_status': [],
        'by_source_site': [],
        'total_file_size': 0,
        'orphan_count': 0,
        'progress': read_progress(),
    }
    conn = _connect()
    if conn is None:
        return stats
    try:
        if _table_exists(conn, 'images'):
            stats['by_status'] = [
                dict(r) for r in conn.execute(
                    'SELECT status, COUNT(*) AS count FROM images GROUP BY status ORDER BY count DESC'
                ).fetchall()
            ]
            stats['by_source_site'] = [
                dict(r) for r in conn.execute(
                    'SELECT source_site, COUNT(*) AS count FROM images GROUP BY source_site ORDER BY count DESC'
                ).fetchall()
            ]
            row = conn.execute(
                "SELECT COALESCE(SUM(file_size),0) AS total FROM images WHERE status='downloaded'"
            ).fetchone()
            stats['total_file_size'] = int(row['total'] or 0)
        if _table_exists(conn, 'orphan_images'):
            row = conn.execute('SELECT COUNT(*) AS c FROM orphan_images').fetchone()
            stats['orphan_count'] = int(row['c'] or 0)
    finally:
        conn.close()
    return stats


def list_images(
    *,
    page: int = 1,
    page_size: int = 60,
    q: str = '',
    status: str = '',
    sort: str = 'sequence',
    order: str = 'asc',
) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(500, page_size))
    sort = sort if sort in SORTABLE else 'sequence'
    order = 'DESC' if str(order).lower() == 'desc' else 'ASC'

    conn = _connect()
    if conn is None or not _table_exists(conn, 'images'):
        if conn:
            conn.close()
        return {'items': [], 'total': 0, 'page': page, 'page_size': page_size}

    try:
        where: list[str] = []
        params: list[Any] = []
        if q:
            where.append('(title LIKE ? OR collection_title LIKE ? OR page_url LIKE ? OR summary LIKE ? OR metadata_text LIKE ?)')
            like = f'%{q}%'
            params.extend([like, like, like, like, like])
        if status:
            where.append('status = ?')
            params.append(status)
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''

        total = int(conn.execute(f'SELECT COUNT(*) AS c FROM images{where_sql}', params).fetchone()['c'])

        cols = ', '.join(IMAGE_COLUMNS)
        offset = (page - 1) * page_size
        rows = conn.execute(
            f'SELECT {cols} FROM images{where_sql} ORDER BY {sort} {order} LIMIT ? OFFSET ?',
            [*params, page_size, offset],
        ).fetchall()
        items = [dict(r) for r in rows]
        return {'items': items, 'total': total, 'page': page, 'page_size': page_size}
    finally:
        conn.close()


def get_image(image_id: str) -> dict[str, Any] | None:
    conn = _connect()
    if conn is None or not _table_exists(conn, 'images'):
        if conn:
            conn.close()
        return None
    try:
        cols = ', '.join(IMAGE_COLUMNS)
        row = conn.execute(f'SELECT {cols} FROM images WHERE id = ?', (image_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve_image_path(image_id: str) -> Path | None:
    """返回图片文件的绝对路径（限定在 ImagesCache 内，防目录穿越）。"""
    record = get_image(image_id)
    if not record:
        return None
    file_path = record.get('file_path') or ''
    file_name = record.get('file_name') or ''
    candidates = []
    if file_path:
        candidates.append(Path(file_path))
    if file_name:
        candidates.append(paths.CACHE_DIR / file_name)
    cache_root = paths.CACHE_DIR.resolve()
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved.exists() and cache_root in resolved.parents:
            return resolved
    return None


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    if conn is None or not _table_exists(conn, 'import_runs'):
        if conn:
            conn.close()
        return []
    try:
        rows = conn.execute(
            'SELECT * FROM import_runs ORDER BY started_at DESC LIMIT ?', (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def read_progress() -> dict[str, Any] | None:
    if not paths.PROGRESS_FILE.exists():
        return None
    try:
        return json.loads(paths.PROGRESS_FILE.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None


def read_final_report() -> str | None:
    if not paths.FINAL_REPORT_FILE.exists():
        return None
    try:
        return paths.FINAL_REPORT_FILE.read_text(encoding='utf-8')
    except OSError:
        return None
