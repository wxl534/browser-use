"""
站点无关的搜索页批量下载编排.

``run_search_page_batch`` 是把 ``download_current_idp_search_page_images``
里那段三阶段流水线拎出来的通用版本.它只跟 ``SiteAdapter`` 打交道:
- Phase 1 顺序解析 search items + manifest(站点差异由 adapter 提供)
- Phase 2 通过 ``ConcurrentImageDownloader`` 并发拉图片字节
- Phase 3 在 ``DOWNLOAD_LOCK`` 内串行落库

记录,去重,校验等工具函数仍住在 ``tools_registry`` 里,本模块以**惰性导入**
的方式拿,避免与之形成 import 循环.
"""
from __future__ import annotations

import asyncio
import json as json_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from browser_use import ActionResult

from adapters.base import SearchPageResult, SiteAdapter
from concurrent_download import ConcurrentImageDownloader, image_download_concurrency, page_delay_seconds


# ---------------------------------------------------------------------------
# 站点状态文件 IO(按 adapter.site_id 命名)
# ---------------------------------------------------------------------------

def _parse_metadata_pairs(metadata_text: str) -> dict:
    """把 ``'标签: 值; 标签2: 值2'`` 形式的 metadata 字符串解析回 {标签: 值} 字典."""
    pairs: dict = {}
    for chunk in str(metadata_text or '').split(';'):
        chunk = chunk.strip()
        if not chunk or ':' not in chunk:
            continue
        key, value = chunk.split(':', 1)
        key = key.strip()
        value = value.strip()
        if key and value and key not in pairs:
            pairs[key] = value
    return pairs


def _merge_overview_metadata(manifest_metadata: str, overview: dict) -> dict:
    """
    合并 IIIF manifest 的 metadata 与详情页 Overview,生成完整的结构化元数据.

    详情页 Overview 优先(更全),manifest 中独有的字段(如 Reading Direction)
    追加在后.两者皆为真实来源,绝不编造.
    """
    merged: dict = {}
    for key, value in (overview or {}).items():
        key = str(key).strip()
        value = str(value).strip()
        if key and value:
            merged[key] = value
    for key, value in _parse_metadata_pairs(manifest_metadata).items():
        if key not in merged:
            merged[key] = value
    return merged


def _site_progress_path(adapter: SiteAdapter, agent_data_dir: Path) -> Path:
    return agent_data_dir / adapter.progress_file_name


def load_site_progress(adapter: SiteAdapter, agent_data_dir: Path) -> dict:
    path = _site_progress_path(adapter, agent_data_dir)
    if not path.exists():
        return {}
    try:
        data = json_module.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (json_module.JSONDecodeError, OSError):
        return {}


def write_site_progress(adapter: SiteAdapter, agent_data_dir: Path, progress: dict) -> Path:
    path = _site_progress_path(adapter, agent_data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_module.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def record_batch_failure(adapter: SiteAdapter, agent_data_dir: Path, reason: str) -> int:
    state = load_site_progress(adapter, agent_data_dir)
    try:
        count = int(state.get('consecutive_batch_failures') or 0)
    except (TypeError, ValueError):
        count = 0
    count += 1
    state['consecutive_batch_failures'] = count
    state['last_batch_failure_reason'] = reason
    state['updated_at'] = datetime.now(timezone.utc).isoformat()
    write_site_progress(adapter, agent_data_dir, state)
    return count


def record_empty_page_event(
    adapter: SiteAdapter,
    agent_data_dir: Path,
    *,
    page_url: str,
    page: int,
    start_index: int,
    total_found: int,
    note: str,
) -> Path:
    event_file = agent_data_dir / adapter.empty_page_events_file_name
    event_file.parent.mkdir(parents=True, exist_ok=True)
    event = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'page_url': page_url,
        'page': page,
        'start_index': start_index,
        'total_found': total_found,
        'note': note,
    }
    with open(event_file, 'a', encoding='utf-8') as file:
        file.write(json_module.dumps(event, ensure_ascii=False) + '\n')
    return event_file


# ---------------------------------------------------------------------------
# 自愈:保证当前 tab 在该站点的搜索结果页 + JS 异常时刷新重试一次
# ---------------------------------------------------------------------------

async def ensure_on_results_page(
    adapter: SiteAdapter,
    browser_session: Any,
    agent_data_dir: Path,
) -> tuple[str, bool, str]:
    """
    确保当前 tab 在 adapter 站点的搜索结果页.否则用 progress 状态推算 canonical
    URL,自动跳转回去.返回 (current_url, navigated, note).
    """
    from tools_registry import _current_browser_url, _navigate_to_image_url

    current_url = await _current_browser_url(browser_session)
    if adapter.is_results_url(current_url):
        return current_url, False, ''
    progress = load_site_progress(adapter, agent_data_dir)
    canonical = adapter.canonical_resume_url(progress)
    if not canonical:
        return current_url, False, ''
    await _navigate_to_image_url(browser_session, canonical)
    await asyncio.sleep(3)
    new_url = await _current_browser_url(browser_session)
    note = f'当前页不是 {adapter.page_label()} 搜索结果页，已自动跳转回 {canonical}（来自 {adapter.progress_file_name}）'
    return new_url or canonical, True, note


async def _hard_reload(browser_session: Any, url: str) -> None:
    """用 CDP ``Page.navigate`` 强制顶层导航.

    当页面 JS 执行上下文已被 Cloudflare/导航摧毁时,``window.location`` 赋值这类
    JS 方式会一起失效;``Page.navigate`` 不依赖页面内 JS 上下文,仍能触发一次干净的
    重新加载并重建执行上下文,是从“整页 JS 都 Uncaught”状态恢复的关键.
    """
    cdp_session = await browser_session.get_or_create_cdp_session()
    await cdp_session.cdp_client.send.Page.navigate(
        params={'url': url},
        session_id=cdp_session.session_id,
    )


async def extract_with_recovery(
    adapter: SiteAdapter,
    browser_session: Any,
    agent_data_dir: Path,
    *,
    max_items: int,
    start_index: int,
) -> tuple[SearchPageResult, str]:
    """``adapter.extract_items`` + 自愈.

    首次提取 JS 失败通常意味着执行上下文被破坏(疑似 Cloudflare 反爬/人机校验).
    此时普通的 ``window.location`` 跳转无效,改用 ``Page.navigate`` 硬重载,并做多次
    退避重试,给 Cloudflare “Just a moment…” 这类会自动放行的挑战页留出通过时间.
    """
    from tools_registry import _current_browser_url

    _, _, nav_note = await ensure_on_results_page(adapter, browser_session, agent_data_dir)
    try:
        page_data = await adapter.extract_items(
            browser_session,
            max_items=max_items,
            start_index=start_index,
        )
        return page_data, nav_note
    except RuntimeError as exc:
        last_exc: Exception = exc

    # 目标 URL:优先当前 URL(上下文已死时探测会返回 ''),否则用进度推算的 canonical 搜索 URL.
    target_url = await _current_browser_url(browser_session)
    if not target_url:
        target_url = adapter.canonical_resume_url(load_site_progress(adapter, agent_data_dir))

    for attempt in range(1, 4):
        if target_url:
            try:
                await _hard_reload(browser_session, target_url)
            except Exception:
                pass
        await asyncio.sleep(min(5 * attempt, 15))  # 退避,等待挑战页自动放行
        try:
            page_data = await adapter.extract_items(
                browser_session,
                max_items=max_items,
                start_index=start_index,
            )
            retry_note = (
                f'首次提取 JS 异常（{last_exc}）；用 Page.navigate 硬重载后第 {attempt} 次重试成功'
            )
            return page_data, (nav_note + ';' + retry_note) if nav_note else retry_note
        except RuntimeError as retry_exc:
            last_exc = retry_exc
            refreshed = await _current_browser_url(browser_session)
            if refreshed:
                target_url = refreshed

    raise RuntimeError(
        '[context_lost] 连续多次提取失败,页面 JS 执行上下文持续不可用'
        '(疑似 Cloudflare 反爬/人机校验,需人工在浏览器中通过验证或更换网络/代理后再续跑).'
        f'最后错误：{last_exc}'
    )


# ---------------------------------------------------------------------------
# 续跑 start_index 校准 + 页码迁移
# ---------------------------------------------------------------------------

def _page_progress_state(adapter: SiteAdapter, agent_data_dir: Path, page: int) -> dict | None:
    from idp_page_progress import load_page_progress

    progress = load_page_progress(agent_data_dir)
    for item in progress.get('pages') or []:
        try:
            if int(item.get('page')) == page:
                return item if isinstance(item, dict) else None
        except (TypeError, ValueError):
            continue
    return None


def effective_start_index(
    adapter: SiteAdapter,
    agent_data_dir: Path,
    requested_start_index: int,
    page: int,
) -> tuple[int, str]:
    """禁止 agent 用 start_index=0 把已推进的页码倒退."""
    try:
        requested = max(0, int(requested_start_index))
    except (TypeError, ValueError):
        requested = 0
    state = _page_progress_state(adapter, agent_data_dir, page)
    if not state:
        return requested, ''
    try:
        progress_index = max(0, int(state.get('next_index') or 0))
    except (TypeError, ValueError):
        progress_index = 0
    effective = max(requested, progress_index)
    if effective != requested:
        return effective, (
            f'已根据 {adapter.page_progress_file_name} 将 start_index '
            f'从 {requested} 提升到 {effective}'
        )
    return effective, ''


# ---------------------------------------------------------------------------
# 主编排函数
# ---------------------------------------------------------------------------

async def run_search_page_batch(
    adapter: SiteAdapter,
    *,
    params: Any,
    browser_session: Any,
) -> ActionResult:
    """
    把当前 tab 上的一整页搜索结果批量下成图片.``params`` 鸭子类型,
    需要的字段见 ``DownloadCurrentIdpSearchPageImagesParams``.

    流水线:
      Phase 1:顺序拉 manifest,解析图片 URL,剔除已下载 URL.
      Phase 2:通过共享 ``ConcurrentImageDownloader`` 并发拉图片字节.
      Phase 3:``DOWNLOAD_LOCK`` 内串行做 sha256 / 去重 / JSONL 追加.
    """
    # 惰性导入:避免与 tools_registry 形成 import 循环
    from idp_page_progress import mark_page_batch_result
    from tools_registry import (
        AGENT_DATA_DIR,
        DOWNLOAD_LOCK,
        IMAGE_DIR,
        _browser_fetch_image_to_file,
        _build_download_record_index,
        _build_existing_image_hash_index,
        _current_browser_url,
        _image_suffix_from_url,
        _max_image_file_sequence,
        _normalize_title,
        _record_generic_image_method_failure,
        _record_generic_image_method_success,
        _record_saved_image_fast,
        _sanitize_allowed_host_suffixes,
        _sha256_file,
        _unique_path,
        _validate_public_image_url,
        format_download_validation_report,
        validate_download_artifacts,
    )

    try:
        record_index = _build_download_record_index(params.record_filename)
        existing_image_hashes = _build_existing_image_hash_index(record_index)
        next_sequence = max(record_index.max_sequence, _max_image_file_sequence(params.file_prefix)) + 1
        remaining = max(0, params.target_count - record_index.downloaded_count)
        if remaining == 0:
            report = format_download_validation_report(validate_download_artifacts(
                target_count=params.target_count,
                record_filename=params.record_filename,
            ))
            return ActionResult(extracted_content='✅ 已达到目标数量,无需继续下载.\n' + report, include_in_memory=True)

        # 反爬节流:每页批量下载前按需等待,降低触发 Cloudflare 限流的概率.
        pacing_delay = page_delay_seconds()
        if pacing_delay > 0:
            await asyncio.sleep(pacing_delay)

        allowed_host_suffixes = _sanitize_allowed_host_suffixes(
            params.allowed_host_suffixes or adapter.default_host_suffixes()
        )
        requested_max_items = min(params.max_items, max(1, remaining))
        batch_item_cap = adapter.batch_item_cap(requested_max_items)
        max_items = min(requested_max_items, batch_item_cap)
        current_url = await _current_browser_url(browser_session)
        _, current_page_from_url, _ = adapter.parse_search_url(current_url)
        start_index, start_index_note = effective_start_index(
            adapter, AGENT_DATA_DIR, params.start_index, current_page_from_url
        )
        try:
            page_data, recovery_note = await extract_with_recovery(
                adapter,
                browser_session,
                AGENT_DATA_DIR,
                max_items=max_items,
                start_index=start_index,
            )
        except RuntimeError as extract_exc:
            failure_count = record_batch_failure(adapter, AGENT_DATA_DIR, f'extract_js_error:{extract_exc}')
            threshold = adapter.consecutive_failure_threshold()
            corrupted = bool(threshold and failure_count >= threshold)
            tag = f'{adapter.site_id}_session_corrupted' if corrupted else f'{adapter.site_id}_extract_failed'
            advice = (
                f'请立刻调用 finish_download_task 结束本次会话，最终数字必须来自 '
                f'{adapter.progress_file_name} / image_record.jsonl；'
                '禁止改为手动点击详情页 / IIIF manifest tab / evaluate 扫 DOM 的方式继续.'
                if corrupted
                else f'请勿改为手动点击详情页；重启浏览器会话后再从 {adapter.page_progress_file_name} 续跑。'
            )
            return ActionResult(
                error=(
                    f'[{tag}] {adapter.page_label()} 搜索页 JS 提取失败：{extract_exc}。'
                    f' consecutive_batch_failures={failure_count}'
                    + (f'/{threshold}' if threshold else '')
                    + f'。{advice}'
                )
            )

        items = page_data.items
        if not items:
            page_url_for_retry = page_data.page_url or current_url
            keyword_for_retry, page_for_retry, limit_for_retry = adapter.parse_search_url(page_url_for_retry)
            # 关键区分:当前页“已全部消费”(start_index 已达到/超过本页 total_found)不是失败,
            # 而是应当翻到下一页的正常信号.早期版本把它误判为 [idp_empty_page] 失败,
            # 又因 task.md 规则禁止在 [idp_empty_page] 后翻页,导致 agent 被永久卡在同一页.
            page_consumed = page_data.total_found > 0 and start_index >= page_data.total_found
            if page_consumed:
                active_page = mark_page_batch_result(
                    AGENT_DATA_DIR,
                    keyword=keyword_for_retry,
                    target_count=params.target_count,
                    page=page_for_retry,
                    start_index=start_index,
                    processed_items=0,
                    downloaded_count=0,
                    skipped_count=0,
                    error_count=0,
                    total_found=page_data.total_found,
                )
                write_site_progress(adapter, AGENT_DATA_DIR, {
                    **load_site_progress(adapter, AGENT_DATA_DIR),
                    'keyword': keyword_for_retry,
                    'current_page': page_for_retry,
                    'next_page': active_page['page'],
                    'next_index': active_page['next_index'],
                    'limit': limit_for_retry,
                    'target_count': params.target_count,
                    'last_error': '',
                    'consecutive_batch_failures': 0,
                    'last_batch_failure_reason': '',
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                })
                return ActionResult(
                    extracted_content=(
                        f'✅ 当前 {adapter.page_label()} 搜索页 page={page_for_retry} 已全部消费'
                        f'（total_found={page_data.total_found}, start_index={start_index}）。\n'
                        f'➡️ 这不是错误，而是正常翻页信号。请调用 '
                        f'navigate_idp_search_page(keyword="{keyword_for_retry}", page={active_page["page"]}, limit={limit_for_retry}) '
                        f'跳到下一页，再调用 download_current_idp_search_page_images 继续下载。'
                    ),
                    include_in_memory=True,
                    long_term_memory=(
                        f'{adapter.page_label()} page={page_for_retry} 已消费完，下一页 page={active_page["page"]}'
                    ),
                )
            event_file = record_empty_page_event(
                adapter,
                AGENT_DATA_DIR,
                page_url=page_url_for_retry,
                page=page_for_retry,
                start_index=start_index,
                total_found=page_data.total_found,
                note='no_items_after_recovery',
            )
            failure_count = record_batch_failure(adapter, AGENT_DATA_DIR, 'empty_search_page_after_recovery')
            threshold = adapter.consecutive_failure_threshold()
            corrupted = bool(threshold and failure_count >= threshold)
            tag = f'{adapter.site_id}_session_corrupted' if corrupted else f'{adapter.site_id}_empty_page'
            progress_after_empty = load_site_progress(adapter, AGENT_DATA_DIR)
            progress_after_empty.update({
                'keyword': keyword_for_retry,
                'current_page': page_for_retry,
                'next_page': page_for_retry,
                'next_index': start_index,
                'limit': limit_for_retry,
                'last_error': 'empty_search_page_after_recovery',
                'empty_page_events_file': str(event_file),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            })
            write_site_progress(adapter, AGENT_DATA_DIR, progress_after_empty)
            advice = (
                '请立刻调用 finish_download_task 结束本次会话,最终数字必须来自 '
                f'{adapter.progress_file_name} / image_record.jsonl；'
                '禁止退化为手动点击.'
                if corrupted
                else f'请重启浏览器会话后从 {adapter.page_progress_file_name} 续跑；不要在当前会话用手动点击替代批量工具。'
            )
            return ActionResult(
                error=(
                    f'[{tag}] 当前 {adapter.page_label()} 搜索页未提取到结果。'
                    f' page={page_for_retry}, start_index={start_index}, '
                    f'body_text_length={page_data.body_text_length}, '
                    f'anchor_count={page_data.anchor_count}, '
                    f'collection_link_count={page_data.collection_link_count}, '
                    f'consecutive_batch_failures={failure_count}'
                    + (f'/{threshold}' if threshold else '')
                    + f'。{advice}'
                )
            )

        keyword, current_page, page_limit = adapter.parse_search_url(page_data.page_url)
        evidence_keyword = keyword or params.title_prefix or adapter.page_label()
        evidence_text = adapter.evidence_template(evidence_keyword)

        downloaded: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        processed_items = 0

        async with DOWNLOAD_LOCK:
            # ---------- Phase 1:顺序解析 item + 过滤已下载 URL ----------
            candidates: list[dict] = []
            remaining_target = max(0, params.target_count - record_index.downloaded_count)
            candidate_cap = max(remaining_target + len(items), max_items)
            for item in items:
                if len(candidates) >= candidate_cap:
                    break
                processed_items += 1
                page_url = str(item.get('url') or '')
                try:
                    resolution = await adapter.resolve_item_image_urls(browser_session, item)
                except Exception as exc:
                    errors.append(f'{page_url}: item 图片解析失败: {exc}')
                    continue

                image_urls: list[str] = []
                for raw_url in resolution.image_urls:
                    try:
                        image_urls.append(_validate_public_image_url(str(raw_url), allowed_host_suffixes))
                    except RuntimeError:
                        continue
                if not image_urls:
                    skipped.append(f'{page_url}: 未找到可下载图片')
                    continue

                overview_meta: dict = {}
                try:
                    overview_meta = await adapter.resolve_item_detail_overview(browser_session, item) or {}
                except Exception as exc:
                    errors.append(f'{page_url}: 详情元数据解析失败(忽略): {exc}')
                    overview_meta = {}
                merged_overview = _merge_overview_metadata(resolution.metadata, overview_meta)

                label = _normalize_title(resolution.label or str(item.get('title') or item.get('id') or f'{adapter.page_label()} item'))
                if merged_overview:
                    metadata_text = '; '.join(f'{key}: {value}' for key, value in merged_overview.items())
                else:
                    metadata_text = (resolution.metadata or '').strip() or '未显示'
                summary_text = (resolution.summary or label or f'{adapter.page_label()} official image').strip()

                per_item_added = 0
                for image_url in image_urls:
                    if per_item_added >= params.images_per_item:
                        break
                    if len(candidates) >= candidate_cap:
                        break
                    existing_record = record_index.records_by_image_url.get(image_url)
                    if existing_record:
                        skipped.append(f'{page_url}: 图片 URL 已记录 #{existing_record.get("sequence")}')
                        continue
                    candidates.append({
                        'page_url': page_url,
                        'image_url': image_url,
                        'label': label,
                        'metadata': metadata_text,
                        'summary': summary_text,
                        'overview': merged_overview,
                        'source_page': current_page,
                    })
                    per_item_added += 1

            # ---------- Phase 2:共享 aiohttp 会话 + 并发拉图片字节 ----------
            async def _fetch_one(index: int, cand: dict) -> dict:
                tmp_target = _unique_path(
                    IMAGE_DIR / f'{params.file_prefix}_pending_{index:04d}{_image_suffix_from_url(cand["image_url"])}'
                )
                try:
                    await downloader.fetch_to_file(
                        cand['image_url'],
                        tmp_target,
                        referer=cand['page_url'],
                    )
                    return {**cand, 'image_path': tmp_target, 'download_method': 'python_direct', 'fetch_error': None}
                except Exception as direct_exc:
                    try:
                        path = await _browser_fetch_image_to_file(
                            browser_session,
                            cand['image_url'],
                            tmp_target.name,
                        )
                        return {
                            **cand,
                            'image_path': path,
                            'download_method': f'browser_context_fetch_after_python_error:{direct_exc}',
                            'fetch_error': None,
                        }
                    except Exception as browser_exc:
                        return {
                            **cand,
                            'image_path': None,
                            'download_method': 'fail',
                            'fetch_error': f'python={direct_exc}; browser={browser_exc}',
                        }

            fetch_results: list[dict] = []
            if candidates:
                async with ConcurrentImageDownloader(timeout_seconds=params.timeout_seconds) as downloader:
                    fetch_results = await asyncio.gather(*(_fetch_one(i, c) for i, c in enumerate(candidates)))

            # ---------- Phase 3:串行 sha256 + 去重 + 落库 ----------
            for result in fetch_results:
                image_url = result['image_url']
                page_url = result['page_url']
                if record_index.downloaded_count >= params.target_count:
                    leftover = result.get('image_path')
                    if leftover:
                        Path(leftover).unlink(missing_ok=True)
                    continue
                if result.get('fetch_error'):
                    _record_generic_image_method_failure('browser_context_fetch', 0, image_url, result['fetch_error'])
                    errors.append(f'{page_url}: 图片下载失败: {result["fetch_error"]}')
                    continue

                image_path: Path = result['image_path']
                try:
                    file_hash = _sha256_file(image_path)
                    existing_content_record = record_index.records_by_file_hash.get(file_hash)
                    if existing_content_record:
                        image_path.unlink(missing_ok=True)
                        skipped.append(f'{page_url}: 图片内容已记录 #{existing_content_record.get("sequence")}')
                        continue
                    existing_image_path = existing_image_hashes.get(file_hash)
                    if existing_image_path and existing_image_path.resolve() != image_path.resolve():
                        image_path.unlink(missing_ok=True)
                        skipped.append(f'{page_url}: image 目录已有相同图片 {existing_image_path.name}')
                        continue

                    while next_sequence in record_index.used_sequences:
                        next_sequence += 1
                    sequence = next_sequence
                    label = result['label']
                    title = _normalize_title(f'{params.title_prefix}_{sequence:03d}_{label}')

                    record_result = await _record_saved_image_fast(
                        image_path=image_path,
                        sequence=sequence,
                        title=title,
                        collection_title=label,
                        page_url=page_url,
                        image_url=image_url,
                        evidence=evidence_text,
                        metadata=result['metadata'],
                        summary=result['summary'],
                        overview=result.get('overview') or {},
                        source_page=result.get('source_page'),
                        record_filename=params.record_filename,
                        info_filename=params.info_filename,
                        record_index=record_index,
                        existing_image_hashes=existing_image_hashes,
                        precomputed_file_hash=file_hash,
                    )
                    if record_result.error:
                        image_path.unlink(missing_ok=True)
                        errors.append(f'{page_url}: 记录失败: {record_result.error}')
                        continue
                    _record_generic_image_method_success(result['download_method'].split(':', 1)[0], sequence, image_url)
                    recorded_file_name = str((record_index.records_by_image_url.get(image_url) or {}).get('file_name') or image_path.name)
                    downloaded.append(f'#{sequence}: {recorded_file_name} | {label} | {page_url} | {result["download_method"]}')
                    next_sequence = max(next_sequence + 1, record_index.max_sequence + 1)
                except Exception as exc:
                    _record_generic_image_method_failure('record_phase', 0, image_url, str(exc))
                    errors.append(f'{page_url}: 图片记录阶段出错: {exc}')

        # ---------- 状态更新 + 报告 ----------
        validation = validate_download_artifacts(
            target_count=params.target_count,
            record_filename=params.record_filename,
            validate_image_files=False,
            include_duplicate_hash_groups=False,
        )
        report = format_download_validation_report(validation)
        report_file = AGENT_DATA_DIR / 'final_download_report.md'
        report_file.write_text(report + '\n', encoding='utf-8')
        total_found = page_data.total_found
        next_index = start_index + processed_items
        next_page = current_page
        if next_index >= total_found:
            next_page = current_page + 1
            next_index = 0
        if processed_items > 0 and not downloaded and (len(errors) + len(skipped)) >= processed_items:
            next_page = current_page + 1
            next_index = 0
        active_page = mark_page_batch_result(
            AGENT_DATA_DIR,
            keyword=keyword,
            target_count=params.target_count,
            page=current_page,
            start_index=start_index,
            processed_items=processed_items,
            downloaded_count=len(downloaded),
            skipped_count=len(skipped),
            error_count=len(errors),
            total_found=total_found,
            last_error='; '.join(errors[:3]),
        )
        progress_file = write_site_progress(adapter, AGENT_DATA_DIR, {
            **load_site_progress(adapter, AGENT_DATA_DIR),
            'keyword': keyword,
            'current_page': current_page,
            'next_page': active_page['page'],
            'next_index': active_page['next_index'],
            'limit': page_limit,
            'target_count': params.target_count,
            'downloaded_records': validation['downloaded_records'],
            'remaining_records': validation['remaining_records'],
            'last_processed_items': processed_items,
            'last_downloaded_count': len(downloaded),
            'last_skipped_count': len(skipped),
            'last_error_count': len(errors),
            'last_search_url': page_data.page_url,
            'page_progress_file': str(AGENT_DATA_DIR / adapter.page_progress_file_name),
            'consecutive_batch_failures': 0,
            'last_batch_failure_reason': '',
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })

        msg = (
            f'✅ {adapter.page_label()} 当前搜索页批量处理完成\n'
            f'- 处理藏品数: {processed_items}/{len(items)}\n'
            f'- 本次新增下载: {len(downloaded)}\n'
            f'- 跳过: {len(skipped)}\n'
            f'- 错误: {len(errors)}\n'
            f'- 当前有效记录: {validation["downloaded_records"]}/{params.target_count}\n'
            f'- 并发下载: {image_download_concurrency()} (env BROWSER_USE_IMAGE_DOWNLOAD_CONCURRENCY)\n'
            f'- 进度文件: {progress_file}\n'
            f'- 下次建议: page={active_page["page"]}, start_index={active_page["next_index"]}\n'
        )
        if requested_max_items != max_items:
            msg += (
                f'- 批量上限: agent 请求 max_items={requested_max_items}，'
                f'已按 {adapter.batch_item_cap_env_var} 限制为 {max_items}\n'
            )
        if start_index_note:
            msg += f'- start_index 修正: {start_index_note}\n'
        if recovery_note:
            msg += f'- 自愈: {recovery_note}\n'
        if downloaded:
            msg += '- downloaded_first_20:\n' + '\n'.join(f'  - {line}' for line in downloaded[:20]) + '\n'
        if skipped:
            msg += '- skipped_first_20:\n' + '\n'.join(f'  - {line}' for line in skipped[:20]) + '\n'
        if errors:
            msg += '- errors_first_20:\n' + '\n'.join(f'  - {line}' for line in errors[:20]) + '\n'
        msg += '\n' + report
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=(
                f'{adapter.page_label()} 批量下载新增 {len(downloaded)} 张，'
                f'当前 {validation["downloaded_records"]}/{params.target_count}'
            ),
        )
    except Exception as e:
        failure_count = record_batch_failure(
            adapter, AGENT_DATA_DIR, f'unhandled_exception:{type(e).__name__}:{e}'
        )
        threshold = adapter.consecutive_failure_threshold()
        corrupted = bool(threshold and failure_count >= threshold)
        tag = f'{adapter.site_id}_session_corrupted' if corrupted else f'{adapter.site_id}_batch_unhandled_error'
        advice = (
            f'请立刻调用 finish_download_task 结束本次会话，最终数字必须来自 '
            f'{adapter.progress_file_name} / image_record.jsonl；'
            '禁止改为手动点击详情页 / IIIF manifest tab / evaluate 扫 DOM 的方式继续.'
            if corrupted
            else '请勿手动 fallback;重启浏览器会话后再继续.'
        )
        return ActionResult(
            error=(
                f'[{tag}] {adapter.page_label()} 当前搜索页批量下载时出错: {str(e)}。'
                f' consecutive_batch_failures={failure_count}'
                + (f'/{threshold}' if threshold else '')
                + f'。{advice}'
            )
        )
