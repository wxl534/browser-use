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


def _iter_record_source_pages(cache_dir: Path, record_file_name: str = 'image_record.jsonl'):
    """逐行读 image_record.jsonl,产出每条记录里有效的 source_page(搜索页码)整数."""
    path = cache_dir / record_file_name
    if not path.exists():
        return
    try:
        handle = open(path, encoding='utf-8')
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            raw = record.get('source_page')
            try:
                page = int(raw)
            except (TypeError, ValueError):
                continue
            if page >= 1:
                yield page


def reconcile_frontier_from_records(
    cache_dir: Path,
    *,
    keyword: str = '',
    target_count: int = 0,
    record_file_name: str = 'image_record.jsonl',
    max_reasonable_page: int = 200,
) -> dict | None:
    """
    用 image_record.jsonl 里每条记录的 ``source_page`` 重建页级 frontier,自愈
    ``idp_page_progress.json``,避免续跑时从低页逐页重走已下载页.

    策略(只抬高下界,绝不回退):
    - 收集所有有记录的搜索页 → frontier = 其最大值.
    - 把 ``page < frontier`` 且有记录的页标记 ``done``(视为已消费,不再重走).
    - ``[1, frontier)`` 区间内**无任何记录的空洞页**显式补成 ``pending``,让续跑
      回头补扫一次(去重保证已下载的不重复;补扫后若 0 新增会被确定性判 done),
      避免「中间页全是重复 → 0 记录 → 被永久跳过」的完整性漏洞.
    - frontier 页保留/置为 ``in_progress`` 且 ``next_index=0``,让续跑只重扫这一页
      补齐尾部 item(去重保证已下载的不会重复),完整性零损失.

    返回 ``{'frontier': N, 'healed_pages': k, 'pages_with_records': m, 'gap_pages': g}``;
    若没有任何带 ``source_page`` 的记录(老数据 / 空记录),返回 ``None``,调用方
    退回到原有 select_next_page 行为.
    """
    pages_with_records = sorted({
        page for page in _iter_record_source_pages(cache_dir, record_file_name)
        if page <= max_reasonable_page
    })
    if not pages_with_records:
        return None

    frontier = pages_with_records[-1]
    progress = load_page_progress(cache_dir)
    if not progress:
        progress = {
            'keyword': keyword,
            'target_count': target_count,
            'active': {'page': frontier, 'next_index': 0},
            'pages': [],
            'created_at': utc_now(),
        }
    if keyword:
        progress['keyword'] = keyword
    if target_count:
        progress['target_count'] = target_count

    healed = 0
    record_page_set = set(pages_with_records)
    for page in pages_with_records:
        state = _ensure_page_state(progress, page, status='pending')
        if page < frontier:
            if state.get('status') not in {'done', 'blocked'}:
                state['status'] = 'done'
                state['reason'] = 'reconciled_from_records'
                state['next_index'] = 0
                state['updated_at'] = utc_now()
                healed += 1
        else:
            # frontier 页:保留为可续(in_progress)以补齐尾部 item;已 done 则不动.
            if state.get('status') not in {'done', 'blocked'}:
                state['status'] = 'in_progress'
                state['next_index'] = 0
                state['updated_at'] = utc_now()

    # 补扫 [1, frontier) 区间内无任何记录的空洞页:显式补成 pending(若未处于终态),
    # 让 select_next_page 回头补扫,杜绝中间页因全重复而 0 记录被永久跳过.
    gap_pages = 0
    for page in range(1, frontier):
        if page in record_page_set:
            continue
        existing = _page_state(progress, page)
        if existing is None:
            _ensure_page_state(progress, page, status='pending')
            gap_pages += 1
        elif existing.get('status') not in {'done', 'blocked', 'in_progress', 'pending'}:
            existing['status'] = 'pending'
            existing['updated_at'] = utc_now()
            gap_pages += 1

    progress['frontier_from_records'] = frontier
    progress['frontier_reconciled_at'] = utc_now()
    write_page_progress(cache_dir, progress)
    return {
        'frontier': frontier,
        'healed_pages': healed,
        'pages_with_records': len(pages_with_records),
        'gap_pages': gap_pages,
    }


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
