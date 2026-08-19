"""
Page-level progress queue for IDP search downloads.

image_record.jsonl is the image-level source of truth. This file manages page
and index state so resume cannot be polluted by LLM jumps to extreme pages.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


PAGE_PROGRESS_FILE = 'idp_page_progress.json'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_page_progress(cache_dir: Path) -> dict:
    path = cache_dir / PAGE_PROGRESS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_page_progress(cache_dir: Path, data: dict) -> Path:
    path = cache_dir / PAGE_PROGRESS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def _page_state(progress: dict, page: int) -> dict | None:
    for item in progress.get('pages') or []:
        try:
            if int(item.get('page')) == page:
                return item
        except (TypeError, ValueError):
            continue
    return None


def _ensure_page_state(progress: dict, page: int, status: str = 'pending') -> dict:
    pages = progress.setdefault('pages', [])
    state = _page_state(progress, page)
    if state is None:
        state = {
            'page': page,
            'status': status,
            'next_index': 0,
            'attempts': 0,
            'downloaded_count': 0,
            'skipped_count': 0,
            'error_count': 0,
            'updated_at': utc_now(),
        }
        pages.append(state)
        pages.sort(key=lambda item: int(item.get('page') or 0))
    return state


def initialize_page_progress(
    cache_dir: Path,
    *,
    keyword: str,
    target_count: int,
    start_page: int = 1,
    start_index: int = 0,
) -> dict:
    progress = load_page_progress(cache_dir)
    if not progress:
        progress = {
            'keyword': keyword,
            'target_count': target_count,
            'active': {'page': start_page, 'next_index': start_index},
            'pages': [],
            'created_at': utc_now(),
        }
    progress['keyword'] = keyword
    progress['target_count'] = target_count
    state = _ensure_page_state(progress, start_page, status='in_progress')
    state['next_index'] = max(0, int(start_index))
    progress['active'] = {'page': start_page, 'next_index': state['next_index']}
    progress['updated_at'] = utc_now()
    write_page_progress(cache_dir, progress)
    return progress


def select_next_page(
    cache_dir: Path,
    *,
    keyword: str,
    target_count: int,
    fallback_page: int = 1,
    max_reasonable_page: int = 200,
) -> dict:
    progress = load_page_progress(cache_dir)
    if not progress:
        progress = initialize_page_progress(
            cache_dir,
            keyword=keyword,
            target_count=target_count,
            start_page=fallback_page,
            start_index=0,
        )

    progress['keyword'] = keyword
    progress['target_count'] = target_count
    pages = progress.setdefault('pages', [])

    for item in pages:
        try:
            page = int(item.get('page'))
        except (TypeError, ValueError):
            continue
        if page > max_reasonable_page and item.get('status') not in {'blocked', 'done'}:
            item['status'] = 'blocked'
            item['reason'] = f'page_over_{max_reasonable_page}'
            item['updated_at'] = utc_now()

    candidates = []
    for item in pages:
        status = str(item.get('status') or 'pending')
        if status in {'in_progress', 'pending', 'failed'}:
            try:
                page = int(item.get('page'))
            except (TypeError, ValueError):
                continue
            if page <= max_reasonable_page:
                candidates.append(item)

    if candidates:
        state = sorted(candidates, key=lambda item: (str(item.get('status')) != 'in_progress', int(item.get('page') or 0)))[0]
    else:
        done_pages = [
            int(item.get('page'))
            for item in pages
            if str(item.get('status')) in {'done', 'blocked'}
            and str(item.get('page') or '').isdigit()
            and int(item.get('page')) <= max_reasonable_page
        ]
        next_page = max(done_pages or [fallback_page - 1]) + 1
        if next_page > max_reasonable_page:
            next_page = fallback_page
        state = _ensure_page_state(progress, next_page, status='pending')

    state['status'] = 'in_progress'
    state.setdefault('next_index', 0)
    state['updated_at'] = utc_now()
    progress['active'] = {
        'page': int(state.get('page') or fallback_page),
        'next_index': int(state.get('next_index') or 0),
    }
    progress['updated_at'] = utc_now()
    write_page_progress(cache_dir, progress)
    return progress['active']


def mark_page_batch_result(
    cache_dir: Path,
    *,
    keyword: str,
    target_count: int,
    page: int,
    start_index: int,
    processed_items: int,
    downloaded_count: int,
    skipped_count: int,
    error_count: int,
    total_found: int,
    last_error: str = '',
) -> dict:
    progress = load_page_progress(cache_dir)
    if not progress:
        progress = initialize_page_progress(cache_dir, keyword=keyword, target_count=target_count, start_page=page)

    progress['keyword'] = keyword
    progress['target_count'] = target_count
    state = _ensure_page_state(progress, page, status='in_progress')
    state['attempts'] = int(state.get('attempts') or 0) + 1
    state['downloaded_count'] = int(state.get('downloaded_count') or 0) + downloaded_count
    state['skipped_count'] = int(state.get('skipped_count') or 0) + skipped_count
    state['error_count'] = int(state.get('error_count') or 0) + error_count
    state['last_processed_items'] = processed_items
    state['last_downloaded_count'] = downloaded_count
    state['last_skipped_count'] = skipped_count
    state['last_error_count'] = error_count
    state['total_found'] = total_found
    if last_error:
        state['last_error'] = last_error

    next_index = max(0, start_index + processed_items)
    if total_found and next_index >= total_found:
        state['status'] = 'done'
        state['next_index'] = 0
        next_page = page + 1
        next_index = 0
    elif processed_items > 0 and downloaded_count == 0 and error_count > 0:
        state['status'] = 'blocked'
        state['reason'] = 'zero_download_with_errors'
        state['next_index'] = next_index
        next_page = page + 1
        next_index = 0
    elif processed_items > 0 and downloaded_count == 0 and skipped_count >= processed_items:
        state['status'] = 'done'
        state['reason'] = 'all_skipped_or_duplicates'
        state['next_index'] = 0
        next_page = page + 1
        next_index = 0
    else:
        state['status'] = 'in_progress'
        state['next_index'] = next_index
        next_page = page

    _ensure_page_state(progress, next_page, status='pending')
    progress['active'] = {'page': next_page, 'next_index': next_index}
    progress['updated_at'] = utc_now()
    state['updated_at'] = progress['updated_at']
    write_page_progress(cache_dir, progress)
    return progress['active']
