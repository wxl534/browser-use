"""`download_image_from_url` 工具：从 tools_registry.py 拆分而来。

共享 helper / 参数模型仍由 tools_registry 提供；运行时全局通过 tr.* 实时读取。
"""
import tools_registry as tr
from tools_registry import (
    ActionResult,
    DOWNLOAD_LOCK,
    DownloadImageFromUrlParams,
    Path,
    _choose_reliable_page_url,
    _clean_url_text,
    _extract_generic_image_candidates,
    _find_existing_image_file_by_hash,
    _get_browser_cookie_header,
    _get_cached_download_index,
    _load_generic_image_strategy,
    _looks_like_iiif_manifest_url,
    _normalize_border_ratio,
    _ordered_generic_image_methods,
    _prefix_from_filename,
    _record_generic_image_method_failure,
    _record_generic_image_method_success,
    _record_saved_image_fast,
    _refresh_generic_index_mtime,
    _renumber_title_if_needed,
    _resolve_generic_image_url,
    _resolve_iiif_manifest_to_image_url,
    _safe_requested_image_sequence_from_index,
    _sanitize_allowed_host_suffixes,
    _save_generic_image_by_method,
    _sha256_file,
    _site_invalid_collection_url,
    _site_manifest_url_from_page_url,
    _source_hash,
    _titled_image_stem,
    _validate_public_image_url,
    tools,
)


@tools.action(
    description=(
        '通用图片下载并记录工具：可自动从当前页面提取公网图片 URL，也可接收明确的图片直链、IIIF manifest、IIIF 大图 URL 或 viewer 图片 URL；'
        '按学习到的优先顺序依次尝试 Python 直连、浏览器上下文 fetch、干净截图裁剪兜底，'
        '保存到 image 目录，并同步写入 image_record.jsonl、title.txt 和 temple_photo_info.md。'
        '适用于 IDP/British Library 等非 Kyohaku 网站，也可作为通用兜底工具。'
    ),
    param_model=DownloadImageFromUrlParams,
)
async def download_image_from_url(params: DownloadImageFromUrlParams, browser_session):
    """
    下载任意公网图片直链并记录元数据，支持自动找图、浏览器 fetch 和干净截图兜底。
    """
    try:
        page_data: dict = {}
        page_url = _clean_url_text(params.page_url)
        extraction_error = ''
        allowed_host_suffixes = _sanitize_allowed_host_suffixes(params.allowed_host_suffixes)
        try:
            page_data = await _extract_generic_image_candidates(browser_session, allowed_host_suffixes)
            page_url, page_url_note = _choose_reliable_page_url(page_url, page_data.get('page_url', ''))
        except Exception as e:
            extraction_error = str(e)
            page_url_note = ''
            if not params.image_url.strip() and not page_url:
                raise RuntimeError(
                    f'当前浏览器无法提取图片候选: {extraction_error}。'
                    '如果日志包含 browser not connected / No valid agent focus，请停止当前任务并重启浏览器会话。'
                ) from e

        if _site_invalid_collection_url(page_url):
            page_url, page_url_note = _choose_reliable_page_url(page_url, '')
        if not page_url and _site_invalid_collection_url(params.page_url) and not params.image_url.strip():
            return ActionResult(error=f'模型传入的详情页 URL 非法（疑似搜索/列表页），且无法从浏览器获取可信当前页: {params.page_url}')

        manifest_url_from_page = _site_manifest_url_from_page_url(page_url)
        preferred_image_url = _clean_url_text(params.image_url) or manifest_url_from_page

        try:
            image_url, candidates = _resolve_generic_image_url(
                preferred_image_url,
                page_url,
                page_data,
                params.image_index,
                allowed_host_suffixes,
            )
        except RuntimeError:
            if not preferred_image_url and params.allow_clean_screenshot:
                image_url = ''
                candidates = []
            else:
                raise
        referer = _clean_url_text(params.referer) or page_url

        manifest_note = page_url_note
        if manifest_url_from_page and not params.image_url.strip():
            manifest_note += f'- 已从详情页 URL 推导 IIIF manifest: {manifest_url_from_page}\n'

        if image_url and _looks_like_iiif_manifest_url(image_url):
            manifest_source_url = image_url
            cookie_header = ''
            if params.use_browser_cookies:
                try:
                    cookie_header = await _get_browser_cookie_header(browser_session, [image_url, page_url])
                except Exception:
                    cookie_header = ''
            try:
                resolved_image_url, manifest_candidate_count = await _resolve_iiif_manifest_to_image_url(
                    image_url,
                    allowed_host_suffixes,
                    params.timeout_seconds,
                    referer or page_url or None,
                    cookie_header or None,
                )
                manifest_note = (
                    manifest_note +
                    f'- IIIF manifest 已解析为图片 URL: {resolved_image_url}\n'
                    f'- Manifest 图片候选数: {manifest_candidate_count}\n'
                )
                image_url = resolved_image_url
            except Exception as iiif_error:
                # 兜底：网站没有 IIIF 逻辑 / manifest 损坏 / 非标准 / 网络失败时，
                # 不让 IIIF 解析失败直接判定整个下载失败，依次回退到页面 DOM 图片直链、
                # 再到 clean_screenshot 截图；都不可用才报错。
                fallback_image_url = ''
                for candidate in candidates:
                    if not candidate or candidate == manifest_source_url:
                        continue
                    if _looks_like_iiif_manifest_url(candidate):
                        continue
                    try:
                        fallback_image_url = _validate_public_image_url(candidate, allowed_host_suffixes)
                        break
                    except Exception:
                        continue
                if fallback_image_url:
                    image_url = fallback_image_url
                    manifest_note += (
                        f'- ⚠️ IIIF manifest 解析失败（{iiif_error}），已回退到页面图片直链: {image_url}\n'
                    )
                elif params.allow_clean_screenshot:
                    image_url = ''
                    manifest_note += (
                        f'- ⚠️ IIIF manifest 解析失败（{iiif_error}）且无可用直链，已回退到 clean_screenshot 截图兜底\n'
                    )
                else:
                    raise RuntimeError(
                        f'IIIF manifest 解析失败且无兜底可用: {iiif_error}。'
                        '可传入图片直链、确保页面已显示大图，或允许 clean_screenshot 兜底。'
                    ) from iiif_error

        record_index, existing_image_hashes, index_cache = _get_cached_download_index(params.record_filename)

        source_hash = _source_hash(page_url, image_url, 0)

        existing_record = record_index.records_by_image_url.get(image_url) if image_url else None
        if existing_record is None and source_hash:
            existing_record = record_index.records_by_source_hash.get(source_hash)
        if existing_record:
            msg = (
                f'✅ 图片来源已有下载记录，视为当前图片已处理，继续下一条即可\n'
                f'- 已有序号: {existing_record.get("sequence")}\n'
                f'- 已有文件: {existing_record.get("file_name", "")}\n'
                f'- 图片 URL: {image_url}\n'
                f'- source_hash: {source_hash}\n'
                f'- 页面 URL: {existing_record.get("page_url", "") or page_url or "未提供"}\n'
                f'{manifest_note}'
            )
            existing_file = tr.IMAGE_DIR / Path(str(existing_record.get('file_name') or '')).name
            attachments = [str(existing_file)] if existing_file.exists() else []
            return ActionResult(
                extracted_content=msg,
                include_in_memory=True,
                long_term_memory=f'图片来源已记录，跳过重复下载: {existing_record.get("file_name", "")}',
                attachments=attachments,
            )

        file_prefix = _prefix_from_filename(params.file_name, 'temple')
        sequence, sequence_note = _safe_requested_image_sequence_from_index(params.sequence, record_index, file_prefix)
        title = _renumber_title_if_needed(params.title, sequence)
        border_ratio = _normalize_border_ratio(params.border_ratio)
        # 临时落地名 = 图片自己的「序号_标题」（信息提取阶段已识别 title），落地瞬间即可读；
        # 落地后只在该词干后追加信息 hash（source_hash），定为最终名，保证图片与信息严格对应。
        file_name = _titled_image_stem(title, sequence)

        strategy_before = _load_generic_image_strategy()
        methods = _ordered_generic_image_methods(strategy_before, params.prefer_browser_fetch, params.allow_clean_screenshot)
        if not image_url:
            methods = ['clean_screenshot'] if params.allow_clean_screenshot else []
        attempt_errors: list[str] = []
        image_path: Path | None = None
        method = ''

        async with DOWNLOAD_LOCK:
            for candidate_method in methods:
                try:
                    image_path = await _save_generic_image_by_method(
                        candidate_method,
                        browser_session,
                        image_url,
                        file_name,
                        page_url,
                        referer,
                        params.timeout_seconds,
                        params.use_browser_cookies,
                        params.black_threshold,
                        params.white_threshold,
                        border_ratio,
                    )
                    method = candidate_method
                    break
                except Exception as e:
                    error_text = str(e)
                    attempt_errors.append(f'{candidate_method}: {error_text}')
                    _record_generic_image_method_failure(candidate_method, sequence, image_url, error_text)

        if image_path is None or not method:
            return ActionResult(error='通用图片下载失败: ' + '; '.join(attempt_errors))

        file_hash = _sha256_file(image_path)
        existing_content_record = record_index.records_by_file_hash.get(file_hash)
        if existing_content_record:
            image_path.unlink(missing_ok=True)
            existing_file = tr.IMAGE_DIR / Path(str(existing_content_record.get('file_name') or '')).name
            attachments = [str(existing_file)] if existing_file.exists() else []
            msg = (
                f'✅ 图片内容已有下载记录，已删除本次重复文件并跳过\n'
                f'- 已有序号: {existing_content_record.get("sequence")}\n'
                f'- 已有文件: {existing_content_record.get("file_name", "")}\n'
                f'- 本次序号: {sequence}\n'
                f'- SHA256: {file_hash}\n'
                f'- 图片 URL: {image_url}\n'
                f'- 页面 URL: {page_url or "未提供"}\n'
                f'{manifest_note}'
            )
            return ActionResult(
                extracted_content=msg,
                include_in_memory=True,
                long_term_memory=f'图片内容已记录，跳过重复下载: {existing_content_record.get("file_name", "")}',
                attachments=attachments,
            )

        existing_image_path = existing_image_hashes.get(file_hash)
        if existing_image_path is None:
            existing_image_path = _find_existing_image_file_by_hash(file_hash, exclude_path=image_path)
        if existing_image_path and existing_image_path.resolve() != image_path.resolve():
            image_path.unlink(missing_ok=True)
            msg = (
                f'✅ image 目录中已存在相同图片内容，已删除本次重复文件并跳过\n'
                f'- 已有文件: {existing_image_path.name}\n'
                f'- 本次序号: {sequence}\n'
                f'- SHA256: {file_hash}\n'
                f'- 图片 URL: {image_url}\n'
                f'- 页面 URL: {page_url or "未提供"}\n'
                f'{manifest_note}'
            )
            return ActionResult(
                extracted_content=msg,
                include_in_memory=True,
                long_term_memory=f'image 目录已有相同图片，跳过重复下载: {existing_image_path.name}',
                attachments=[str(existing_image_path)],
            )

        downloaded_count_before = record_index.downloaded_count
        record_result = await _record_saved_image_fast(
            image_path=image_path,
            sequence=sequence,
            title=title,
            collection_title=params.collection_title,
            page_url=page_url,
            image_url=image_url,
            evidence=params.evidence,
            metadata=params.metadata,
            summary=params.summary,
            record_filename=params.record_filename,
            info_filename=params.info_filename,
            record_index=record_index,
            existing_image_hashes=existing_image_hashes,
            precomputed_file_hash=file_hash,
        )
        _refresh_generic_index_mtime(index_cache)
        if record_result.error:
            if image_path and image_path.exists():
                image_path.unlink(missing_ok=True)
            return record_result
        if record_index.downloaded_count == downloaded_count_before:
            # 落库环节内部判定为重复（content/source hash），已删除本次文件并跳过。
            return record_result

        final_record = record_index.records[-1]
        image_path = tr.IMAGE_DIR / Path(str(final_record.get('file_name') or image_path.name)).name

        strategy_after = _record_generic_image_method_success(method, sequence, image_url)
        strategy_note = (
            f'- 方法策略: 当前方法 {method} 连续成功 {strategy_after.get("streak", 0)} 次'
            f'{f"，已锁定后续优先使用 {strategy_after.get("preferred_method")}" if strategy_after.get("preferred_method") else ""}\n'
        )
        fallback_note = f'- 失败后切换记录: {"; ".join(attempt_errors)}\n' if attempt_errors else ''
        msg = (
            f'✅ 已下载并记录图片 #{sequence}: {image_path.name}\n'
            f'{sequence_note + chr(10) if sequence_note else ""}'
            f'- 下载方式: {method}\n'
            f'- 图片 URL: {image_url}\n'
            f'- 页面 URL: {page_url or "未提供"}\n'
            f'- 文件大小: {image_path.stat().st_size} bytes\n'
            f'- 页面候选数: {len(candidates)}\n'
            f'{manifest_note}'
            f'{f"- 页面候选提取失败但已使用传入 URL: {extraction_error}\\n" if extraction_error else ""}'
            f'{strategy_note}'
            f'{fallback_note}'
            f'{record_result.extracted_content or ""}'
        )
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'已下载并记录图片 #{sequence}: {image_path.name}',
            attachments=[str(image_path)],
        )
    except Exception as e:
        return ActionResult(error=f'通用图片下载并记录时出错: {str(e)}')
