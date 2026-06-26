"""LOC / Kyohaku 站点特有工具（从 tools_registry.py 物理迁出）。

这些工具默认不注册（受 tools_registry.legacy_tools_action 把关，需
BROWSER_USE_ENABLE_LEGACY_TOOLS=1 才进入 agent 工具目录）。共享 helper /
运行时全局仍由 tools_registry 提供，本模块只负责承载 LOC/Kyohaku 专有逻辑，
保持主 tools_registry.py 干净。IDP 仍留在主流程，不在此处。

本文件由 _extract_legacy.py 自动生成式迁移，保持原始定义顺序。
"""
from __future__ import annotations

import tools_registry as tr  # noqa: F401  (运行时全局通过 tr.* 实时读取)
from tools_registry import (
    AGENT_DATA_DIR,
    ActionResult,
    BaseModel,
    DOWNLOAD_LOCK,
    Field,
    IMAGE_DIR,
    Path,
    RecordDownloadedImageParams,
    _browser_fetch_image_to_file,
    _current_tiff_files,
    _download_file,
    _download_image_to_file,
    _existing_recorded_image_urls,
    _get_browser_cookie_header,
    _image_record_file,
    _image_suffix_from_url,
    _load_json_list,
    _load_jsonl_records,
    _navigate_to_image_url,
    _next_available_image_sequence,
    _normalize_border_ratio,
    _normalize_title,
    _numbered_file_stem,
    _prefix_from_filename,
    _renumber_title_if_needed,
    _safe_agent_data_filename,
    _safe_requested_image_sequence,
    _save_clean_visible_image_screenshot,
    _write_json_list,
    asyncio,
    datetime,
    json_module,
    legacy_tools_action,
    os,
    record_downloaded_image,
    timezone,
    urljoin,
    urlparse,
)


KYOHAKU_METHODS = ('python_direct', 'browser_context_fetch', 'clean_screenshot')


def _append_download_title(title: str, title_file: Path | None = None) -> Path:
    """
    Legacy LOC/Kyohaku 专用：将成功下载的图片标题追加写入 title.txt。
    （通用核心已移除 title.txt，本函数仅供默认关闭的 legacy 工具使用。）
    """
    target_file = title_file or AGENT_DATA_DIR / 'title.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)

    normalized_title = _normalize_title(title)
    existing_content = target_file.read_text(encoding='utf-8') if target_file.exists() else ''
    prefix = '' if not existing_content or existing_content.endswith('\n') else '\n'
    with open(target_file, 'a', encoding='utf-8') as f:
        f.write(f'{prefix}{normalized_title}\n')

    return target_file


KYOHAKU_STRATEGY_LOCK_THRESHOLD = 5


class CollectLocResultsParams(BaseModel):
    """收集 LOC 搜索结果 URL 队列的参数模型"""
    limit: int = Field(default=25, ge=1, le=100, description='最多收集多少条当前页面结果')
    queue_filename: str = Field(default='loc_result_queue.json', description='保存到 browseruse_agent_data 的队列文件名')


class CollectKyohakuResultsParams(BaseModel):
    """收集 Kyohaku 搜索结果 URL 队列的参数模型"""
    limit: int = Field(default=100, ge=1, le=300, description='最多收集多少条当前页面结果')
    queue_filename: str = Field(default='kyohaku_result_queue.json', description='保存到 browseruse_agent_data 的队列文件名')


class GetNextLocQueueItemParams(BaseModel):
    """读取下一个待处理 LOC 队列项的参数模型"""
    queue_filename: str = Field(default='loc_result_queue.json', description='browseruse_agent_data 中的队列文件名')
    mark_in_progress: bool = Field(default=True, description='返回后是否把该条目标记为 in_progress，避免重复领取')


class GetNextKyohakuQueueItemParams(BaseModel):
    """读取下一个待处理 Kyohaku 队列项的参数模型"""
    queue_filename: str = Field(default='kyohaku_result_queue.json', description='browseruse_agent_data 中的队列文件名')
    mark_in_progress: bool = Field(default=True, description='返回后是否把该条目标记为 in_progress，避免重复领取')


class MarkLocQueueItemParams(BaseModel):
    """更新 LOC 队列项状态的参数模型"""
    url: str = Field(description='要更新状态的 LOC item URL')
    status: str = Field(description='新状态：pending、in_progress、downloaded、skipped 或 failed')
    error: str | None = Field(default=None, description='失败或跳过原因；成功/待处理状态会清理旧 error')
    queue_filename: str = Field(default='loc_result_queue.json', description='browseruse_agent_data 中的队列文件名')


class MarkKyohakuQueueItemParams(BaseModel):
    """更新 Kyohaku 队列项状态的参数模型"""
    url: str = Field(description='要更新状态的 Kyohaku 藏品 URL')
    status: str = Field(description='新状态：pending、in_progress、downloaded、skipped 或 failed')
    error: str | None = Field(default=None, description='失败或跳过原因；成功/待处理状态会清理旧 error')
    queue_filename: str = Field(default='kyohaku_result_queue.json', description='browseruse_agent_data 中的队列文件名')


class RebuildLocDownloadStateParams(BaseModel):
    """重建 LOC 下载状态的参数模型"""
    remove_irrelevant: bool = Field(default=True, description='是否从队列中移除明显不相关的 LOC 条目')
    reset_in_progress: bool = Field(default=True, description='是否把运行中断留下的 in_progress 重置为 pending')
    rewrite_title_file: bool = Field(default=True, description='是否根据成功下载记录重写 title.txt，并自动备份旧文件')


class DownloadKyohakuImageParams(BaseModel):
    """直接下载京都国立博物馆图片并记录元数据的参数模型"""
    sequence: int = Field(ge=1, description='图片序号，从 1 开始，应与保存文件名顺序一致')
    file_name: str = Field(description='保存文件名或基础名，例如 temple_001；扩展名会优先使用原图 URL 的扩展名')
    title: str = Field(description='用于最终重命名的短标题，例如 寺_001_藏品标题_图1')
    collection_title: str = Field(default='', description='藏品页面显示的原始标题')
    page_url: str = Field(default='', description='藏品详情页 URL；为空时使用当前页面 URL')
    image_url: str = Field(default='', description='原始图片 URL；为空时工具会从当前 DOM 自动查找 /art_images/ 图片')
    image_index: int = Field(default=0, ge=0, description='当页面有多个 /art_images/ 图片时选择第几个，按大图优先排序')
    evidence: str = Field(default='', description='判断与关键词相关的证据')
    metadata: str = Field(default='', description='作者、时代、分类、馆藏号等信息')
    summary: str = Field(default='', description='图片或藏品的简短中文说明')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')
    timeout_seconds: int = Field(default=180, ge=30, le=900, description='直接下载超时时间')


class CleanKyohakuScreenshotParams(BaseModel):
    """打开 Kyohaku 原图页后精确截图并记录元数据的参数模型"""
    sequence: int = Field(ge=1, description='图片序号，从 1 开始，应与保存文件名顺序一致')
    file_name: str = Field(description='截图保存文件名或基础名，例如 temple_001；默认尽量沿用原图扩展名')
    title: str = Field(description='用于最终重命名的短标题，例如 寺_001_藏品标题_图1')
    collection_title: str = Field(default='', description='藏品页面显示的原始标题')
    page_url: str = Field(default='', description='藏品详情页 URL')
    image_url: str = Field(default='', description='原图页 URL，例如 https://knmdb.kyohaku.go.jp/art_images/...-L.jpg；为空时使用当前页面')
    evidence: str = Field(default='', description='判断与关键词相关的证据')
    metadata: str = Field(default='', description='作者、时代、分类、馆藏号等信息')
    summary: str = Field(default='', description='图片或藏品的简短中文说明')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')
    black_threshold: int = Field(default=18, ge=0, le=80, description='自动去黑边阈值，越大越容易裁掉深色边框')
    white_threshold: int = Field(default=245, ge=180, le=255, description='自动去白边阈值，越小越容易裁掉浅色边框')
    border_ratio: float = Field(default=0.985, description='一整行/列超过该比例为黑色或白色时才视为边框；工具会把异常值归一化到 0.90-0.999')
    prefer_native_download: bool = Field(default=True, description='如果提供了原图 URL，优先保存原始图片字节，不重新截图编码')
    preserve_source_format: bool = Field(default=True, description='截图裁剪后是否沿用原图 URL 的扩展名重新编码；否则输出 PNG')


class SaveKyohakuImageViaBrowserParams(BaseModel):
    """使用浏览器页面上下文保存 Kyohaku 图片并记录元数据的参数模型"""
    sequence: int = Field(ge=1, description='图片序号，从 1 开始，应与保存文件名顺序一致')
    file_name: str = Field(description='保存文件名或基础名，例如 temple_001；扩展名会优先使用原图 URL 的扩展名')
    title: str = Field(description='用于最终重命名的短标题，例如 china_temple_001_藏品标题_图1')
    collection_title: str = Field(default='', description='藏品页面显示的原始标题')
    page_url: str = Field(default='', description='藏品详情页 URL；为空时使用当前页面 URL')
    image_url: str = Field(default='', description='原始图片 URL；为空时工具会从当前 DOM 自动查找 /art_images/ 图片')
    image_index: int = Field(default=0, ge=0, description='当页面有多个 /art_images/ 图片时选择第几个，按大图优先排序')
    evidence: str = Field(default='', description='判断与关键词相关的证据')
    metadata: str = Field(default='', description='作者、时代、分类、馆藏号等信息')
    summary: str = Field(default='', description='图片或藏品的简短中文说明')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')


class DownloadCurrentKyohakuItemImagesParams(BaseModel):
    """批量下载当前 Kyohaku 藏品全部图片并记录元数据的参数模型"""
    start_sequence: int = Field(ge=1, description='本藏品第一张图片的全局序号，从 1 开始')
    max_images: int = Field(default=50, ge=1, le=300, description='当前藏品最多下载多少张图片')
    file_prefix: str = Field(default='temple', description='保存文件名前缀，例如 temple 会生成 temple_001')
    title_prefix: str = Field(default='china_temple', description='重命名标题前缀，例如 china_temple')
    collection_title: str = Field(description='藏品页面显示的原始标题')
    page_url: str = Field(default='', description='藏品详情页 URL；为空时使用当前页面 URL')
    evidence: str = Field(default='', description='判断与关键词相关的证据')
    metadata: str = Field(default='', description='作者、时代、分类、馆藏号等信息')
    summary: str = Field(default='', description='图片或藏品的简短中文说明')
    skip_existing_urls: bool = Field(default=True, description='是否跳过 image_record.jsonl 中已经成功记录过的 image_url')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')
    timeout_seconds: int = Field(default=180, ge=30, le=900, description='单张图片保存超时时间')


def _record_download_status(record: dict, record_file: Path | None = None) -> Path:
    """
    追加结构化下载记录，便于恢复和审计。
    """
    target_file = record_file or AGENT_DATA_DIR / 'download_record.jsonl'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        **record,
    }
    with open(target_file, 'a', encoding='utf-8') as f:
        f.write(json_module.dumps(payload, ensure_ascii=False) + '\n')
    return target_file


async def _click_selected_download_button(browser_session) -> None:
    """
    在当前详情页点击已选格式对应的 Go 按钮，作为 Python 直连被 403/520 拒绝时的兜底。
    """
    js_code = '''
    (function() {
        const select = document.querySelector('select[id^="select-resource"]');
        if (!select) {
            return { success: false, error: "页面中未找到下载格式选择器(select-resource)" };
        }
        const container = select.closest('.input-group-small') || select.parentElement;
        let goButton = container ? container.querySelector('button') : null;
        if (!goButton) {
            goButton = document.querySelector('button.button-default');
        }
        if (!goButton) {
            return { success: false, error: "未找到Go按钮" };
        }
        goButton.click();
        return { success: true };
    })()
    '''
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', '点击Go按钮失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', '点击Go按钮失败'))


async def _wait_for_browser_tiff_download(output_dir: Path, before: dict[Path, float], timeout_seconds: int) -> Path:
    """
    只轮询文件系统等待浏览器下载完成，不读取 browser-use 的下载状态，避免 watchdog 卡死。
    """
    loop = asyncio.get_running_loop()
    grace_seconds = int(os.environ.get('BROWSER_USE_DOWNLOAD_GRACE_SECONDS', '120'))
    deadline = loop.time() + timeout_seconds
    grace_deadline = deadline + max(0, grace_seconds)
    last_candidate: Path | None = None
    last_size = -1
    stable_checks = 0

    while loop.time() < grace_deadline:
        active_downloads = list(output_dir.glob('*.crdownload')) + list(output_dir.glob('*.tmp'))
        current = _current_tiff_files(output_dir)
        candidates = [
            path
            for path, mtime in current.items()
            if path not in before or mtime > before.get(path, 0)
        ]
        if candidates:
            candidate = max(candidates, key=lambda path: path.stat().st_mtime)
            current_size = candidate.stat().st_size
            if candidate == last_candidate and current_size == last_size and current_size > 0:
                stable_checks += 1
            else:
                stable_checks = 0
            if not active_downloads and stable_checks >= 1:
                return candidate
            last_candidate = candidate
            last_size = current_size
        elif loop.time() >= deadline and not active_downloads:
            break

        if loop.time() >= deadline and not active_downloads and last_candidate and last_candidate.exists():
            current_size = last_candidate.stat().st_size
            if current_size > 0 and current_size == last_size:
                return last_candidate
        await asyncio.sleep(1)

    if last_candidate and last_candidate.exists() and last_candidate.stat().st_size > 0:
        return last_candidate
    raise RuntimeError(f'浏览器兜底下载超时，{timeout_seconds + grace_seconds} 秒内未发现完成的 TIFF 文件')


def _match_late_download_file(record: dict, image_files: list[Path], used_paths: set[Path]) -> Path | None:
    """
    为已经记为 failed、但浏览器稍后完成落盘的 TIFF 找回文件。
    """
    download_url = str(record.get('url') or '')
    url_stem = Path(urlparse(download_url).path).stem.lower()
    if not url_stem:
        return None

    candidates = [
        path
        for path in image_files
        if path not in used_paths and url_stem in path.stem.lower() and path.stat().st_size > 0
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _reconcile_late_download_records(records: list[dict], record_file: Path, image_dir: Path) -> list[dict]:
    """
    浏览器大文件可能在工具超时后才完成下载；重建状态时把这类文件补记为 downloaded。
    """
    image_files = [
        path
        for pattern in ('*.tif', '*.tiff')
        for path in image_dir.glob(pattern)
        if path.is_file()
    ]
    downloaded_keys = {
        (
            _canonical_loc_url(str(record.get('page_url') or '')),
            str(record.get('url') or ''),
        )
        for record in records
        if record.get('status') == 'downloaded'
    }
    used_paths = {
        Path(str(record.get('file_path'))).resolve()
        for record in records
        if record.get('status') == 'downloaded' and record.get('file_path')
    }

    reconciled_records: list[dict] = []
    for record in records:
        if record.get('status') != 'failed':
            continue
        key = (_canonical_loc_url(str(record.get('page_url') or '')), str(record.get('url') or ''))
        if key in downloaded_keys:
            continue
        late_file = _match_late_download_file(record, image_files, used_paths)
        if not late_file:
            continue
        used_paths.add(late_file.resolve())
        reconciled_records.append({
            'title': record.get('title') or record.get('page_url') or record.get('url') or 'untitled',
            'url': record.get('url', ''),
            'page_url': record.get('page_url', ''),
            'status': 'downloaded',
            'method': 'browser_fallback_late',
            'file_path': str(late_file),
            'file_size': late_file.stat().st_size,
            'reconciled_from': 'failed',
            'previous_error': record.get('error', ''),
        })

    for record in reconciled_records:
        _record_download_status(record, record_file=record_file)

    return [*records, *reconciled_records]


def _queue_file_path(queue_filename: str = 'loc_result_queue.json') -> Path:
    """
    获取队列文件路径，并限制文件名只能落在 browseruse_agent_data 下。
    """
    safe_name = Path((queue_filename or '').strip()).name
    if safe_name in {'', '.', '.loc_result_queue.json', 'loc_result_queue'} or not safe_name.endswith('.json'):
        safe_name = 'loc_result_queue.json'
    return AGENT_DATA_DIR / safe_name


def _canonical_kyohaku_url(url: str) -> str:
    parsed = urlparse((url or '').strip())
    if not parsed.netloc:
        return (url or '').strip()
    path = parsed.path.rstrip('/')
    if path.endswith('.html'):
        return f'{parsed.scheme or "https"}://{parsed.netloc}{path}'
    return f'{parsed.scheme or "https"}://{parsed.netloc}{path}'


def _canonical_loc_url(url: str) -> str:
    """
    规范化 LOC item URL，避免 query/hash/尾斜杠差异导致队列 URL 匹配失败。
    """
    parsed = urlparse((url or '').strip())
    path = parsed.path.rstrip('/')
    if '/item/' in path:
        item_id = path.split('/item/', 1)[1].split('/', 1)[0]
        return f'https://www.loc.gov/item/{item_id}/'
    return (url or '').strip().rstrip('/')


def _looks_relevant_loc_item(title: str, url: str = '') -> bool:
    """
    粗过滤明显不属于 buddhist temple 图片任务的 LOC 条目，避免队列被错误搜索结果污染。
    """
    text = f'{title} {url}'.lower()
    if not text.strip():
        return False
    include_patterns = (
        'buddh',
        'temple',
        'monaster',
        'datsan',
        'pagoda',
        'shrine',
        'lhasa',
        'sikkim',
        'tibet',
        'potala',
        'samye',
        'gyantse',
        'kyoto',
        'tokyo',
        'nikko',
    )
    exclude_patterns = (
        'yankee stadium',
        'scripture facts',
        'code of federal regulations',
        'copyright record book',
        'capitalist transformation',
        'committee',
    )
    return any(pattern in text for pattern in include_patterns) and not any(
        pattern in text for pattern in exclude_patterns
    )


async def _extract_current_kyohaku_item_image_urls(browser_session) -> dict:
    """
    按当前藏品页面顺序提取所有大图 URL。
    优先读取隐藏 viewer 中的 L 图，其次读取主图、缩略图 data-i 和普通 img src。
    """
    js_code = '''
    (function() {
        try {
            const ordered = [];
            const seen = new Set();
            const addUrl = (raw, source) => {
                if (!raw) return;
                String(raw).split(',').forEach(part => {
                    const value = part.trim().split(/\\s+/)[0];
                    if (!value || value.startsWith('data:') || value.startsWith('blob:')) return;
                    try {
                        let url = new URL(value, window.location.href).href;
                        if (!url.includes('/art_images/')) return;
                        url = url.replace(/-S\\.(jpg|jpeg|png|webp)([?#].*)?$/i, '-L.$1$2');
                        url = url.replace(/-M\\.(jpg|jpeg|png|webp)([?#].*)?$/i, '-L.$1$2');
                        if (!seen.has(url)) {
                            seen.add(url);
                            ordered.push({url, source});
                        }
                    } catch (_) {}
                });
            };

            document.querySelectorAll('#_viewer img[src*="/art_images/"]').forEach((img) => addUrl(img.src || img.getAttribute('src'), 'viewer'));
            document.querySelectorAll('#_main_img[src*="/art_images/"]').forEach((img) => addUrl(img.currentSrc || img.src || img.getAttribute('src'), 'main'));
            document.querySelectorAll('img[data-i*="/art_images/"]').forEach((img) => addUrl(img.getAttribute('data-i'), 'thumbnail-data-i'));
            document.querySelectorAll('img[src*="/art_images/"], source[srcset*="/art_images/"]').forEach((element) => {
                addUrl(element.currentSrc);
                addUrl(element.src);
                addUrl(element.srcset);
                addUrl(element.getAttribute('src'));
                addUrl(element.getAttribute('srcset'));
            });
            document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"]').forEach((meta) => addUrl(meta.content, 'meta'));

            return {
                success: true,
                page_url: window.location.href,
                page_title: document.title,
                image_urls: ordered.map(item => item.url),
                candidates: ordered,
            };
        } catch (error) {
            return {success: false, error: error.message, stack: error.stack};
        }
    })()
    '''
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', '批量提取藏品图片 URL 失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', '批量提取藏品图片 URL 失败'))
    return data


async def _extract_kyohaku_image_candidates(browser_session) -> dict:
    js_code = '''
    (function() {
        try {
            const candidates = new Set();
            const addUrl = (raw) => {
                if (!raw) return;
                String(raw).split(',').forEach(part => {
                    const value = part.trim().split(/\\s+/)[0];
                    if (!value || value.startsWith('data:') || value.startsWith('blob:')) return;
                    try {
                        const normalized = new URL(value, window.location.href).href;
                        if (normalized.includes('/art_images/')) candidates.add(normalized);
                    } catch (_) {}
                });
            };

            document.querySelectorAll('img, source, a').forEach((element) => {
                addUrl(element.currentSrc);
                addUrl(element.src);
                addUrl(element.srcset);
                addUrl(element.href);
                addUrl(element.getAttribute('data-src'));
                addUrl(element.getAttribute('data-original'));
            });

            document.querySelectorAll('*').forEach((element) => {
                const bg = getComputedStyle(element).backgroundImage || '';
                for (const match of bg.matchAll(/url\\(["']?([^"')]+)["']?\\)/g)) {
                    addUrl(match[1]);
                }
            });

            const html = document.documentElement.outerHTML;
            for (const match of html.matchAll(/(?:https?:)?\\/\\/[^"'<>\\s]+\\/art_images\\/[^"'<>\\s]+|\\/art_images\\/[^"'<>\\s]+/g)) {
                addUrl(match[0]);
            }

            const urls = Array.from(candidates).sort((a, b) => {
                const score = (url) => {
                    let value = 0;
                    if (/-L\\./i.test(url)) value += 100;
                    if (/-M\\./i.test(url)) value += 50;
                    if (/thumb|thumbnail|small/i.test(url)) value -= 100;
                    if (/\\.jpe?g($|[?#])/i.test(url)) value += 10;
                    return value;
                };
                return score(b) - score(a) || a.localeCompare(b);
            });

            return {
                success: true,
                page_url: window.location.href,
                page_title: document.title,
                image_urls: urls,
            };
        } catch (error) {
            return {success: false, error: error.message, stack: error.stack};
        }
    })()
    '''
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', '提取图片 URL 失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', '提取图片 URL 失败'))
    return data


def _queue_status_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get('status') or 'pending')
        counts[status] = counts.get(status, 0) + 1
    return counts


def _update_result_queue_status(page_url: str, status: str, error: str | None = None) -> None:
    """
    更新结果队列中的当前详情页状态，便于分批恢复。
    """
    if not page_url:
        return
    queue_file = _queue_file_path()
    items = _load_json_list(queue_file)
    changed = False
    target_url = _canonical_loc_url(page_url)
    for item in items:
        if _canonical_loc_url(item.get('url', '')) == target_url:
            item['status'] = status
            if error:
                item['error'] = error
            else:
                item.pop('error', None)
            item['updated_at'] = datetime.now(timezone.utc).isoformat()
            changed = True
            break
    if changed:
        _write_json_list(queue_file, items)


def _update_kyohaku_queue_status(page_url: str, status: str, error: str | None = None, queue_filename: str = 'kyohaku_result_queue.json') -> None:
    """
    更新 Kyohaku 队列中的当前详情页状态，便于分批恢复。
    """
    if not page_url:
        return
    queue_file = _queue_file_path(queue_filename)
    items = _load_json_list(queue_file)
    changed = False
    target_url = _canonical_kyohaku_url(page_url)
    for item in items:
        if _canonical_kyohaku_url(item.get('url', '')) == target_url:
        	item['status'] = status
        	if error:
        		item['error'] = error
        	else:
        		item.pop('error', None)
        	item['updated_at'] = datetime.now(timezone.utc).isoformat()
        	changed = True
        	break
    if changed:
        _write_json_list(queue_file, items)


def _record_kyohaku_download_failure(
    sequence: int,
    title: str,
    collection_title: str,
    page_url: str,
    image_url: str,
    error: str,
    record_filename: str = 'kyohaku_failed_record.jsonl',
) -> None:
    """
    持久化 Kyohaku 下载失败记录，避免失败只停留在 ActionResult 中。
    """
    record_file = _image_record_file(record_filename)
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'sequence': sequence,
        'title': title,
        'collection_title': collection_title,
        'page_url': page_url,
        'image_url': image_url,
        'status': 'failed',
        'error': error,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    with open(record_file, 'a', encoding='utf-8') as f:
        f.write(json_module.dumps(record, ensure_ascii=False) + '\n')


@legacy_tools_action(
    description='收集当前 LOC 搜索结果页中的详情页标题和 URL，保存为可恢复的 browseruse_agent_data/loc_result_queue.json 队列，避免滚动和重复点击。',
    param_model=CollectLocResultsParams,
)
async def collect_loc_result_queue(params: CollectLocResultsParams, browser_session):
    """
    从当前 LOC 搜索结果页提取详情页 URL 队列。
    """
    try:
        js_code = '''
        (function() {
                const anchors = Array.from(document.querySelectorAll('a[href*="/item/"]'));
                const seen = new Set();
                const results = [];
                for (const anchor of anchors) {
                    const href = anchor.href || anchor.getAttribute('href') || '';
                    let title = (anchor.textContent || anchor.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
                    const resultItem = anchor.closest('.item, .search-results-item, article, li');
                    const heading = resultItem ? resultItem.querySelector('h2, h3, .item-title, [class*="title"]') : null;
                    if (heading && heading.textContent) {
                        title = heading.textContent.replace(/\\s+/g, ' ').trim();
                    }
                    const itemUrl = new URL(href, window.location.href).href;
                    const combined = `${title} ${itemUrl}`.toLowerCase();
                    const relevant = /(buddh|temple|monaster|datsan|pagoda|shrine|lhasa|sikkim|tibet|potala|samye|gyantse|kyoto|tokyo|nikko)/i.test(combined);
                    const irrelevant = /(yankee stadium|scripture facts|code of federal regulations|copyright record book|capitalist transformation|committee)/i.test(combined);
                    const invalidTitle = !title
                        || title.length > 240
                        || /function\\s*\\(|jquery|document\\.queryselector|var\\s+/i.test(title);
                    if (!itemUrl.match(/\\/item\\/[^/?#]+\\/?(?:[?#].*)?$/) || invalidTitle || !relevant || irrelevant || seen.has(itemUrl)) {
                        continue;
                    }
                    seen.add(itemUrl);
                    results.push({
                        title,
                        url: itemUrl,
                        source_page: window.location.href,
                    });
                }
            return { success: true, url: window.location.href, title: document.title, results };
        })()
        '''
        cdp_session = await browser_session.get_or_create_cdp_session()
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
            session_id=cdp_session.session_id,
        )
        if result.get('exceptionDetails'):
            error_text = result['exceptionDetails'].get('text', '未知JS错误')
            return ActionResult(error=f'JavaScript执行失败: {error_text}')

        data = result.get('result', {}).get('value')
        if not data or not data.get('success'):
            return ActionResult(error='未能收集 LOC 搜索结果队列')

        queue_file = _queue_file_path(params.queue_filename)
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_json_list(queue_file)
        by_url = {item.get('url'): item for item in existing if item.get('url')}

        added = 0
        for item in data.get('results', [])[:params.limit]:
            url = item.get('url', '')
            title = _normalize_title(item.get('title', ''), fallback=url)
            if not url or url in by_url or not _looks_relevant_loc_item(title, url):
                continue
            by_url[url] = {
                'title': title,
                'url': url,
                'source_page': item.get('source_page') or data.get('url', ''),
                'status': 'pending',
            }
            added += 1

        ordered = list(by_url.values())
        _write_json_list(queue_file, ordered)

        msg = f'✅ 已收集 LOC 结果队列：新增 {added} 条，共 {len(ordered)} 条，文件 {queue_file}'
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'LOC结果队列已更新：新增{added}条，共{len(ordered)}条',
        )
    except Exception as e:
        return ActionResult(error=f'收集 LOC 结果队列时出错: {str(e)}')


@legacy_tools_action(
    description='从 browseruse_agent_data/loc_result_queue.json 中返回下一个 pending 的 LOC item，并可自动标记为 in_progress；不要让 agent 直接读 JSON 文件。',
    param_model=GetNextLocQueueItemParams,
)
async def get_next_loc_queue_item(params: GetNextLocQueueItemParams):
    """
    返回下一个待处理队列项，避免 agent 直接读文件失败后回退到手动点击页面。
    """
    try:
        queue_file = _queue_file_path(params.queue_filename)
        items = _load_json_list(queue_file)
        if not items:
            return ActionResult(error=f'队列为空或不存在: {queue_file}')

        for index, item in enumerate(items):
            status = str(item.get('status') or 'pending')
            if status == 'pending':
                if params.mark_in_progress:
                    item['status'] = 'in_progress'
                    item.pop('error', None)
                    item['updated_at'] = datetime.now(timezone.utc).isoformat()
                    _write_json_list(queue_file, items)

                payload = {
                    'index': index,
                    'title': item.get('title', ''),
                    'url': item.get('url', ''),
                    'status': item.get('status', status),
                    'source_page': item.get('source_page', ''),
                    'queue_file': str(queue_file),
                    'counts': _queue_status_counts(items),
                }
                msg = '✅ 下一个 LOC 队列项:\n' + json_module.dumps(payload, ensure_ascii=False, indent=2)
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory=f"下一个待处理 LOC item: {payload['title']} {payload['url']}",
                )

        counts = _queue_status_counts(items)
        return ActionResult(
            extracted_content='✅ 队列中没有 pending 项:\n' + json_module.dumps(counts, ensure_ascii=False, indent=2),
            include_in_memory=True,
            long_term_memory=f'LOC 队列没有 pending 项，状态统计: {counts}',
        )
    except Exception as e:
        return ActionResult(error=f'读取下一个 LOC 队列项时出错: {str(e)}')


@legacy_tools_action(
    description='把 LOC 队列中的指定 URL 标记为 pending/in_progress/downloaded/skipped/failed，并清理或写入 error；用于无 TIFF、页面异常或手动跳过后的状态同步。',
    param_model=MarkLocQueueItemParams,
)
async def mark_loc_queue_item(params: MarkLocQueueItemParams):
    """
    显式更新队列项状态，避免无 TIFF 项被反复处理。
    """
    allowed_statuses = {'pending', 'in_progress', 'downloaded', 'skipped', 'failed'}
    status = params.status.strip().lower()
    if status not in allowed_statuses:
        return ActionResult(error=f'不支持的队列状态: {params.status}，可用: {", ".join(sorted(allowed_statuses))}')

    try:
        queue_file = _queue_file_path(params.queue_filename)
        items = _load_json_list(queue_file)
        if not items:
            return ActionResult(error=f'队列为空或不存在: {queue_file}')

        target_url = _canonical_loc_url(params.url)
        for item in items:
            if _canonical_loc_url(item.get('url', '')) == target_url:
                item['status'] = status
                if params.error:
                    item['error'] = params.error
                else:
                    item.pop('error', None)
                item['updated_at'] = datetime.now(timezone.utc).isoformat()
                _write_json_list(queue_file, items)

                payload = {
                    'title': item.get('title', ''),
                    'url': item.get('url', ''),
                    'status': item.get('status', ''),
                    'error': item.get('error', ''),
                    'counts': _queue_status_counts(items),
                }
                msg = '✅ 已更新 LOC 队列项:\n' + json_module.dumps(payload, ensure_ascii=False, indent=2)
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory=f"LOC 队列项已标记为 {status}: {item.get('title', '')}",
                )

        items.append({
            'title': target_url,
            'url': target_url,
            'status': status,
            'error': params.error or 'URL 不在当前队列中，已追加状态记录以避免重复处理',
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
        _write_json_list(queue_file, items)
        payload = {
            'title': target_url,
            'url': target_url,
            'status': status,
            'error': params.error or 'URL 不在当前队列中，已追加状态记录以避免重复处理',
            'counts': _queue_status_counts(items),
        }
        msg = '✅ URL 不在队列中，已追加并标记状态:\n' + json_module.dumps(payload, ensure_ascii=False, indent=2)
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'LOC 队列缺失 URL，已追加并标记为 {status}: {target_url}',
        )
    except Exception as e:
        return ActionResult(error=f'更新 LOC 队列项时出错: {str(e)}')


@legacy_tools_action(
	description='收集当前 Kyohaku 搜索结果页中的藏品详情页标题和 URL，保存为可恢复的 browseruse_agent_data/kyohaku_result_queue.json 队列，避免靠滚动反复寻找结果。',
	param_model=CollectKyohakuResultsParams,
)
async def collect_kyohaku_result_queue(params: CollectKyohakuResultsParams, browser_session):
	"""
	从当前 Kyohaku 搜索结果页提取藏品详情页 URL 队列。
	"""
	try:
		js_code = r'''
		(function() {
			const anchors = Array.from(document.querySelectorAll('a[href]'));
			const seen = new Set();
			const results = [];
			for (const anchor of anchors) {
				const rawHref = anchor.getAttribute('href') || anchor.href || '';
				let itemUrl = '';
				try {
					itemUrl = new URL(rawHref, window.location.href).href;
				} catch {
					continue;
				}
				const url = new URL(itemUrl);
				if (!url.hostname.endsWith('kyohaku.go.jp')) {
					continue;
				}
				if (!/\/(?:eng|jp)\/\d+\.html$/i.test(url.pathname)) {
					continue;
				}
				const noisy = /(search|list|index|top|result)/i.test(url.pathname);
				if (noisy || seen.has(itemUrl)) {
					continue;
				}
				const container = anchor.closest('li, tr, article, .item, .result, [class*="result"], [class*="item"]');
				const heading = container ? container.querySelector('h1, h2, h3, h4, [class*="title"], [class*="name"]') : null;
				let title = ((heading && heading.textContent) || anchor.textContent || anchor.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
				if (!title || title.length > 240) {
					title = itemUrl;
				}
				seen.add(itemUrl);
				results.push({
					title,
					url: itemUrl,
					source_page: window.location.href,
				});
			}
			return { success: true, url: window.location.href, title: document.title, results };
		})()
		'''
		cdp_session = await browser_session.get_or_create_cdp_session()
		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
			session_id=cdp_session.session_id,
		)
		if result.get('exceptionDetails'):
			error_text = result['exceptionDetails'].get('text', '未知JS错误')
			return ActionResult(error=f'JavaScript执行失败: {error_text}')

		data = result.get('result', {}).get('value')
		if not data or not data.get('success'):
			return ActionResult(error='未能收集 Kyohaku 搜索结果队列')

		queue_file = _queue_file_path(params.queue_filename)
		queue_file.parent.mkdir(parents=True, exist_ok=True)
		existing = _load_json_list(queue_file)
		by_url = {_canonical_kyohaku_url(item.get('url', '')): item for item in existing if item.get('url')}

		added = 0
		for item in data.get('results', [])[:params.limit]:
			url = _canonical_kyohaku_url(item.get('url', ''))
			title = _normalize_title(item.get('title', ''), fallback=url)
			if not url or url in by_url:
				continue
			by_url[url] = {
				'title': title,
				'url': url,
				'source_page': item.get('source_page') or data.get('url', ''),
				'status': 'pending',
			}
			added += 1

		ordered = list(by_url.values())
		_write_json_list(queue_file, ordered)
		msg = f'✅ 已收集 Kyohaku 结果队列：新增 {added} 条，共 {len(ordered)} 条，文件 {queue_file}'
		return ActionResult(
			extracted_content=msg,
			include_in_memory=True,
			long_term_memory=f'Kyohaku结果队列已更新：新增{added}条，共{len(ordered)}条',
		)
	except Exception as e:
		return ActionResult(error=f'收集 Kyohaku 结果队列时出错: {str(e)}')


@legacy_tools_action(
	description='从 browseruse_agent_data/kyohaku_result_queue.json 中返回下一个 pending 的 Kyohaku 藏品，并可自动标记为 in_progress。',
	param_model=GetNextKyohakuQueueItemParams,
)
async def get_next_kyohaku_queue_item(params: GetNextKyohakuQueueItemParams):
	"""
	返回下一个待处理 Kyohaku 队列项。
	"""
	try:
		queue_file = _queue_file_path(params.queue_filename)
		items = _load_json_list(queue_file)
		if not items:
			return ActionResult(error=f'队列为空或不存在: {queue_file}')

		for index, item in enumerate(items):
			status = str(item.get('status') or 'pending')
			if status == 'pending':
				if params.mark_in_progress:
					item['status'] = 'in_progress'
					item.pop('error', None)
					item['updated_at'] = datetime.now(timezone.utc).isoformat()
					_write_json_list(queue_file, items)

				payload = {
					'index': index,
					'title': item.get('title', ''),
					'url': item.get('url', ''),
					'status': item.get('status', status),
					'source_page': item.get('source_page', ''),
					'queue_file': str(queue_file),
					'counts': _queue_status_counts(items),
				}
				msg = '✅ 下一个 Kyohaku 队列项:\n' + json_module.dumps(payload, ensure_ascii=False, indent=2)
				return ActionResult(
					extracted_content=msg,
					include_in_memory=True,
					long_term_memory=f"下一个待处理 Kyohaku item: {payload['title']} {payload['url']}",
				)

		counts = _queue_status_counts(items)
		return ActionResult(
			extracted_content='✅ Kyohaku 队列中没有 pending 项:\n' + json_module.dumps(counts, ensure_ascii=False, indent=2),
			include_in_memory=True,
			long_term_memory=f'Kyohaku 队列没有 pending 项，状态统计: {counts}',
		)
	except Exception as e:
		return ActionResult(error=f'读取下一个 Kyohaku 队列项时出错: {str(e)}')


@legacy_tools_action(
	description='把 Kyohaku 队列中的指定 URL 标记为 pending/in_progress/downloaded/skipped/failed，并清理或写入 error。',
	param_model=MarkKyohakuQueueItemParams,
)
async def mark_kyohaku_queue_item(params: MarkKyohakuQueueItemParams):
	"""
	显式更新 Kyohaku 队列项状态。
	"""
	allowed_statuses = {'pending', 'in_progress', 'downloaded', 'skipped', 'failed'}
	status = params.status.strip().lower()
	if status not in allowed_statuses:
		return ActionResult(error=f'不支持的队列状态: {params.status}，可用: {", ".join(sorted(allowed_statuses))}')

	try:
		queue_file = _queue_file_path(params.queue_filename)
		items = _load_json_list(queue_file)
		if not items:
			return ActionResult(error=f'队列为空或不存在: {queue_file}')

		target_url = _canonical_kyohaku_url(params.url)
		for item in items:
			if _canonical_kyohaku_url(item.get('url', '')) == target_url:
				item['status'] = status
				if params.error:
					item['error'] = params.error
				else:
					item.pop('error', None)
				item['updated_at'] = datetime.now(timezone.utc).isoformat()
				_write_json_list(queue_file, items)
				payload = {
					'title': item.get('title', ''),
					'url': item.get('url', ''),
					'status': item.get('status', ''),
					'error': item.get('error', ''),
					'counts': _queue_status_counts(items),
				}
				msg = '✅ 已更新 Kyohaku 队列项:\n' + json_module.dumps(payload, ensure_ascii=False, indent=2)
				return ActionResult(
					extracted_content=msg,
					include_in_memory=True,
					long_term_memory=f"Kyohaku 队列项已标记为 {status}: {item.get('title', '')}",
				)

		items.append({
			'title': target_url,
			'url': target_url,
			'status': status,
			'error': params.error or 'URL 不在当前队列中，已追加状态记录以避免重复处理',
			'updated_at': datetime.now(timezone.utc).isoformat(),
		})
		_write_json_list(queue_file, items)
		payload = {
			'title': target_url,
			'url': target_url,
			'status': status,
			'error': params.error or 'URL 不在当前队列中，已追加状态记录以避免重复处理',
			'counts': _queue_status_counts(items),
		}
		msg = '✅ URL 不在队列中，已追加并标记状态:\n' + json_module.dumps(payload, ensure_ascii=False, indent=2)
		return ActionResult(
			extracted_content=msg,
			include_in_memory=True,
			long_term_memory=f'Kyohaku 队列缺失 URL，已追加并标记为 {status}: {target_url}',
		)
	except Exception as e:
		return ActionResult(error=f'更新 Kyohaku 队列项时出错: {str(e)}')


@legacy_tools_action(
    description='合并并清理 LOC 队列文件，过滤无关条目，按 download_record.jsonl 和 image 目录重建下载状态、标题和清单。',
    param_model=RebuildLocDownloadStateParams,
)
async def rebuild_loc_download_state(params: RebuildLocDownloadStateParams):
    """
    修复多轮失败/续跑后产生的队列污染和 title/download_record/image 不一致问题。
    """
    try:
        data_dir = AGENT_DATA_DIR
        image_dir = IMAGE_DIR
        main_queue = _queue_file_path()
        candidate_names = ('loc_result_queue.json', '.loc_result_queue.json', 'loc_result_queue')
        merged_by_url: dict[str, dict] = {}
        removed_irrelevant = 0

        for name in candidate_names:
            queue_file = data_dir / name
            for item in _load_json_list(queue_file):
                url = _canonical_loc_url(str(item.get('url', '')))
                if not url:
                    continue
                title = _normalize_title(str(item.get('title') or url), fallback=url)
                if params.remove_irrelevant and not _looks_relevant_loc_item(title, url):
                    removed_irrelevant += 1
                    continue
                item['url'] = url
                item['title'] = title
                status = str(item.get('status') or 'pending')
                if params.reset_in_progress and status == 'in_progress':
                    item['status'] = 'pending'
                    item.pop('error', None)
                if item.get('status') in {'pending', 'downloaded'}:
                    item.pop('error', None)

                existing = merged_by_url.get(url)
                if existing is None:
                    merged_by_url[url] = item
                else:
                    rank = {'downloaded': 4, 'failed': 3, 'skipped': 2, 'pending': 1, 'in_progress': 0}
                    if rank.get(str(item.get('status')), 0) > rank.get(str(existing.get('status')), 0):
                        merged_by_url[url] = item

        record_file = data_dir / 'download_record.jsonl'
        records = _reconcile_late_download_records(
            _load_jsonl_records(record_file),
            record_file,
            image_dir,
        )
        downloaded_records_by_url: dict[str, dict] = {}
        for record in records:
            page_url = _canonical_loc_url(str(record.get('page_url') or record.get('url') or ''))
            if not page_url:
                continue
            status = str(record.get('status') or '')
            title = _normalize_title(str(record.get('title') or page_url), fallback=page_url)
            if params.remove_irrelevant and not _looks_relevant_loc_item(title, page_url):
                continue

            item = merged_by_url.setdefault(page_url, {'title': title, 'url': page_url, 'status': 'pending'})
            if status == 'downloaded':
                item['status'] = 'downloaded'
                item['title'] = title
                item['file_path'] = record.get('file_path', item.get('file_path', ''))
                item.pop('error', None)
                downloaded_records_by_url[page_url] = record
            elif item.get('status') != 'downloaded' and status in {'failed', 'skipped'}:
                item['status'] = status
                if record.get('error'):
                    item['error'] = record.get('error')

        queue_items = list(merged_by_url.values())
        _write_json_list(main_queue, queue_items)
        for stale_name in ('.loc_result_queue.json', 'loc_result_queue'):
            stale_file = data_dir / stale_name
            if stale_file.exists():
                stale_file.unlink()

        image_files = sorted(
            [
                path
                for pattern in ('*.tif', '*.tiff')
                for path in image_dir.glob(pattern)
                if path.is_file()
            ],
            key=lambda path: path.stat().st_mtime,
        )
        inventory = {
            'rebuilt_at': datetime.now(timezone.utc).isoformat(),
            'queue_counts': _queue_status_counts(queue_items),
            'removed_irrelevant': removed_irrelevant,
            'downloaded_record_count': len(downloaded_records_by_url),
            'image_file_count': len(image_files),
            'image_files': [
                {'name': path.name, 'path': str(path), 'size': path.stat().st_size}
                for path in image_files
            ],
            'downloaded_records': list(downloaded_records_by_url.values()),
        }
        inventory_file = data_dir / 'download_inventory.json'
        inventory_file.write_text(json_module.dumps(inventory, ensure_ascii=False, indent=2), encoding='utf-8')

        if params.rewrite_title_file:
            title_file = data_dir / 'title.txt'
            if title_file.exists():
                backup_file = data_dir / 'title.txt.bak'
                backup_file.write_text(title_file.read_text(encoding='utf-8'), encoding='utf-8')
            titles = [
                _normalize_title(str(record.get('title') or record.get('page_url') or record.get('url') or 'untitled'))
                for record in downloaded_records_by_url.values()
            ]
            title_file.write_text('\n'.join(titles + ['END', '']), encoding='utf-8')

        msg = (
            '✅ 已重建 LOC 下载状态\n'
            f'- 队列文件: {main_queue}\n'
            f'- 队列状态: {json_module.dumps(_queue_status_counts(queue_items), ensure_ascii=False)}\n'
            f'- 移除无关项: {removed_irrelevant}\n'
            f'- 成功下载记录: {len(downloaded_records_by_url)}\n'
            f'- image 中 TIFF 文件: {len(image_files)}\n'
            f'- 清单: {inventory_file}'
        )
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'LOC 状态已重建: {_queue_status_counts(queue_items)}',
        )
    except Exception as e:
        return ActionResult(error=f'重建 LOC 下载状态时出错: {str(e)}')


async def _record_saved_kyohaku_image(
    image_path: Path,
    sequence: int,
    title: str,
    collection_title: str,
    page_url: str,
    image_url: str,
    evidence: str,
    metadata: str,
    summary: str,
    record_filename: str,
    info_filename: str,
) -> ActionResult:
    return await record_downloaded_image(params=RecordDownloadedImageParams(
        sequence=sequence,
        file_name=image_path.name,
        title=title,
        collection_title=collection_title,
        page_url=page_url,
        image_url=image_url,
        evidence=evidence,
        metadata=metadata,
        summary=summary,
        record_filename=record_filename,
        info_filename=info_filename,
    ))


def _resolve_kyohaku_image_url(params_image_url: str, page_url: str, page_data: dict, image_index: int) -> tuple[str, list[str]]:
    candidates = page_data.get('image_urls', []) or []
    if params_image_url.strip():
        return urljoin(page_url or page_data.get('page_url', ''), params_image_url.strip()), candidates

    if not candidates:
        raise RuntimeError('当前页面未找到 /art_images/ 原图 URL；请先打开详情页大图或图片列表。')
    if image_index >= len(candidates):
        raise RuntimeError(f'图片索引 {image_index} 超出范围，当前找到 {len(candidates)} 个原图候选。')
    return candidates[image_index], candidates


def _validate_kyohaku_image_url(image_url: str) -> None:
    parsed_host = urlparse(image_url).netloc.lower()
    if not parsed_host.endswith('kyohaku.go.jp'):
        raise RuntimeError(f'拒绝下载非 Kyohaku 官方域名图片: {image_url}')


def _kyohaku_strategy_file() -> Path:
    return AGENT_DATA_DIR / 'kyohaku_download_strategy.json'


def _load_kyohaku_strategy() -> dict:
    strategy_file = _kyohaku_strategy_file()
    default = {
        'last_method': '',
        'streak': 0,
        'preferred_method': '',
        'history': [],
    }
    if not strategy_file.exists():
        return default
    try:
        data = json_module.loads(strategy_file.read_text(encoding='utf-8'))
    except json_module.JSONDecodeError:
        return default
    if not isinstance(data, dict):
        return default
    return {**default, **data}


def _write_kyohaku_strategy(strategy: dict) -> Path:
    strategy_file = _kyohaku_strategy_file()
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text(json_module.dumps(strategy, ensure_ascii=False, indent=2), encoding='utf-8')
    return strategy_file


def _ordered_kyohaku_methods(strategy: dict) -> list[str]:
    preferred = str(strategy.get('preferred_method') or '')
    if preferred in KYOHAKU_METHODS:
        return [preferred, *[method for method in KYOHAKU_METHODS if method != preferred]]
    return list(KYOHAKU_METHODS)


def _record_kyohaku_method_success(method: str, sequence: int, image_url: str) -> dict:
    strategy = _load_kyohaku_strategy()
    if method == strategy.get('last_method'):
        streak = int(strategy.get('streak') or 0) + 1
    else:
        streak = 1

    strategy['last_method'] = method
    strategy['streak'] = streak
    if streak >= KYOHAKU_STRATEGY_LOCK_THRESHOLD:
        strategy['preferred_method'] = method

    history = strategy.get('history')
    if not isinstance(history, list):
        history = []
    history.append({
        'sequence': sequence,
        'method': method,
        'image_url': image_url,
        'status': 'success',
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })
    strategy['history'] = history[-50:]
    _write_kyohaku_strategy(strategy)
    return strategy


def _record_kyohaku_method_failure(method: str, sequence: int, image_url: str, error: str) -> dict:
    strategy = _load_kyohaku_strategy()
    if strategy.get('preferred_method') == method:
        strategy['preferred_method'] = ''
        strategy['streak'] = 0

    history = strategy.get('history')
    if not isinstance(history, list):
        history = []
    history.append({
        'sequence': sequence,
        'method': method,
        'image_url': image_url,
        'status': 'failed',
        'error': error,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })
    strategy['history'] = history[-50:]
    _write_kyohaku_strategy(strategy)
    return strategy


async def _save_kyohaku_by_method(
    method: str,
    browser_session,
    image_url: str,
    file_name: str,
    page_url: str,
    timeout_seconds: int,
) -> Path:
    if method == 'python_direct':
        cookie_header = await _get_browser_cookie_header(browser_session, [image_url, page_url])
        return await _download_image_to_file(
            image_url,
            file_name,
            timeout_seconds,
            referer=page_url,
            cookies=cookie_header,
        )

    if method == 'browser_context_fetch':
        return await _browser_fetch_image_to_file(browser_session, image_url, file_name)

    if method == 'clean_screenshot':
        await _navigate_to_image_url(browser_session, image_url)
        image_path, _ = await _save_clean_visible_image_screenshot(
            browser_session,
            file_name,
            black_threshold=18,
            white_threshold=245,
            border_ratio=0.985,
            preferred_ext=_image_suffix_from_url(image_url),
        )
        return image_path

    raise RuntimeError(f'未知 Kyohaku 图片保存方法: {method}')


@legacy_tools_action(
    description=(
        '京都国立博物馆/KNMDB 专用：优先从当前页面 DOM 自动提取 /art_images/ 原图 URL，'
        '用 Python 直接下载到 image 目录，并同步写入 image_record.jsonl、title.txt 和 temple_photo_info.md。'
        '优先使用这个工具，只有找不到原图 URL 时才退回 smart_screenshot。'
    ),
    param_model=DownloadKyohakuImageParams,
)
async def download_kyohaku_image(params: DownloadKyohakuImageParams, browser_session):
    """
    直接下载 KNMDB 原图并记录元数据，避免截图裁剪包含边框、按钮或页面背景。
    """
    try:
        page_data = await _extract_kyohaku_image_candidates(browser_session)
        page_url = params.page_url.strip() or page_data.get('page_url', '')
        try:
            image_url, candidates = _resolve_kyohaku_image_url(
                params.image_url,
                page_url,
                page_data,
                params.image_index,
            )
            _validate_kyohaku_image_url(image_url)
        except Exception as e:
            return ActionResult(error=f'{e} 找不到时再用 clean_kyohaku_screenshot 兜底。')

        file_prefix = _prefix_from_filename(params.file_name, 'temple')
        sequence, sequence_note = _safe_requested_image_sequence(params.sequence, params.record_filename, file_prefix)
        file_name = _numbered_file_stem(params.file_name, sequence, file_prefix)
        title = _renumber_title_if_needed(params.title, sequence)

        strategy_before = _load_kyohaku_strategy()
        method_order = _ordered_kyohaku_methods(strategy_before)
        attempt_errors: list[str] = []
        image_path: Path | None = None
        method = ''

        for candidate_method in method_order:
            try:
                image_path = await _save_kyohaku_by_method(
                    candidate_method,
                    browser_session,
                    image_url,
                    file_name,
                    page_url,
                    params.timeout_seconds,
                )
                method = candidate_method
                break
            except Exception as e:
                error_text = str(e)
                attempt_errors.append(f'{candidate_method}: {error_text}')
                _record_kyohaku_method_failure(candidate_method, sequence, image_url, error_text)

        if image_path is None or not method:
            error_text = '所有 Kyohaku 图片保存方法都失败: ' + '; '.join(attempt_errors)
            _record_kyohaku_download_failure(
               sequence=sequence,
               title=title,
               collection_title=params.collection_title,
               page_url=page_url,
               image_url=image_url,
               error=error_text,
            )
            _update_kyohaku_queue_status(page_url, 'failed', error_text)
            return ActionResult(error=error_text)

        record_result = await _record_saved_kyohaku_image(
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
        )
        if record_result.error:
            return record_result

        strategy_after = _record_kyohaku_method_success(method, sequence, image_url)
        _update_kyohaku_queue_status(page_url, 'downloaded')
        strategy_note = (
            f'- 方法策略: 当前方法 {method} 连续成功 {strategy_after.get("streak", 0)} 次'
            f'{f"，已锁定后续优先使用 {strategy_after.get("preferred_method")}" if strategy_after.get("preferred_method") else ""}\n'
        )
        fallback_note = f'- 失败后切换记录: {"; ".join(attempt_errors)}\n' if attempt_errors else ''
        msg = (
            f'✅ 已直接下载 Kyohaku 原图 #{sequence}: {image_path.name}\n'
            f'{sequence_note + chr(10) if sequence_note else ""}'
            f'- 下载方式: {method}\n'
            f'- 原图 URL: {image_url}\n'
            f'- 页面 URL: {page_url}\n'
            f'- 文件大小: {image_path.stat().st_size} bytes\n'
            f'- DOM 候选数: {len(candidates)}\n'
            f'{strategy_note}'
            f'{fallback_note}'
            f'{record_result.extracted_content or ""}'
        )
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'已直接下载并记录 Kyohaku 图片 #{sequence}: {image_path.name}',
            attachments=[str(image_path)],
        )
    except Exception as e:
        return ActionResult(error=f'直接下载 Kyohaku 图片时出错: {str(e)}')


@legacy_tools_action(
    description=(
        'Kyohaku 当前藏品批量下载工具：在当前藏品详情页或图片列表页一次性提取该藏品全部 /art_images/ 大图 URL，'
        '按页面顺序批量保存并写入 image_record.jsonl、title.txt 和 temple_photo_info.md。'
        '优先用这个工具处理多图藏品，避免 agent 一张张调用或手动猜 URL。'
    ),
    param_model=DownloadCurrentKyohakuItemImagesParams,
)
async def download_current_kyohaku_item_images(params: DownloadCurrentKyohakuItemImagesParams, browser_session):
    """
    批量下载当前藏品的全部图片。
    """
    try:
        page_data = await _extract_current_kyohaku_item_image_urls(browser_session)
        page_url = params.page_url.strip() or page_data.get('page_url', '')
        image_urls = [str(url) for url in page_data.get('image_urls', []) if url]
        if not image_urls:
            return ActionResult(error='当前藏品页面未提取到 /art_images/ 大图 URL；请先打开详情页或“See all available images”。')

        existing_urls = _existing_recorded_image_urls(params.record_filename) if params.skip_existing_urls else set()
        successes: list[dict] = []
        failures: list[dict] = []
        safe_start = _next_available_image_sequence(params.record_filename, params.file_prefix)
        requested_start = params.start_sequence
        next_sequence = max(requested_start, safe_start)
        sequence_note = ''
        if requested_start < safe_start:
            sequence_note = f'⚠️ agent 传入 start_sequence={requested_start} 已落后于现有进度，已自动从 {next_sequence} 开始'

        for item_index, image_url in enumerate(image_urls, 1):
            if len(successes) >= params.max_images:
                break
            if params.skip_existing_urls and image_url in existing_urls:
                continue

            try:
                _validate_kyohaku_image_url(image_url)
                file_name = f'{params.file_prefix}_{next_sequence:03d}'
                image_path: Path | None = None
                method = ''
                attempt_errors: list[str] = []
                strategy_before = _load_kyohaku_strategy()

                for candidate_method in _ordered_kyohaku_methods(strategy_before):
                    try:
                        image_path = await _save_kyohaku_by_method(
                            candidate_method,
                            browser_session,
                            image_url,
                            file_name,
                            page_url,
                            params.timeout_seconds,
                        )
                        method = candidate_method
                        break
                    except Exception as e:
                        error_text = str(e)
                        attempt_errors.append(f'{candidate_method}: {error_text}')
                        _record_kyohaku_method_failure(candidate_method, next_sequence, image_url, error_text)

                if image_path is None or not method:
                    error_text = '; '.join(attempt_errors)
                    failures.append({'sequence': next_sequence, 'image_url': image_url, 'error': error_text})
                    _record_kyohaku_download_failure(
                        sequence=next_sequence,
                        title='',
                        collection_title=params.collection_title,
                        page_url=page_url,
                        image_url=image_url,
                        error=error_text,
                    )
                    next_sequence += 1
                    continue

                safe_collection_title = _normalize_title(params.collection_title, fallback='untitled')
                title = _normalize_title(f'{params.title_prefix}_{next_sequence:03d}_{safe_collection_title}_图{item_index}')
                summary = params.summary.strip()
                if summary:
                    summary = f'{summary}（当前藏品第 {item_index} 张图像）'
                else:
                    summary = f'{safe_collection_title} 的第 {item_index} 张图像。'

                record_result = await _record_saved_kyohaku_image(
                    image_path=image_path,
                    sequence=next_sequence,
                    title=title,
                    collection_title=params.collection_title,
                    page_url=page_url,
                    image_url=image_url,
                    evidence=params.evidence,
                    metadata=params.metadata,
                    summary=summary,
                    record_filename=params.record_filename,
                    info_filename=params.info_filename,
                )
                if record_result.error:
                    failures.append({'sequence': next_sequence, 'image_url': image_url, 'error': record_result.error})
                    _record_kyohaku_download_failure(
                        sequence=next_sequence,
                        title=title,
                        collection_title=params.collection_title,
                        page_url=page_url,
                        image_url=image_url,
                        error=record_result.error,
                    )
                    next_sequence += 1
                    continue

                strategy_after = _record_kyohaku_method_success(method, next_sequence, image_url)
                successes.append({
                    'sequence': next_sequence,
                    'file_name': image_path.name,
                    'image_url': image_url,
                    'method': method,
                    'streak': strategy_after.get('streak', 0),
                })
                existing_urls.add(image_url)
                next_sequence += 1
            except Exception as e:
                error_text = str(e)
                failures.append({'sequence': next_sequence, 'image_url': image_url, 'error': error_text})
                _record_kyohaku_download_failure(
                    sequence=next_sequence,
                    title='',
                    collection_title=params.collection_title,
                    page_url=page_url,
                    image_url=image_url,
                    error=error_text,
                )
                next_sequence += 1

        if successes:
            _update_kyohaku_queue_status(page_url, 'downloaded')
        elif failures:
            _update_kyohaku_queue_status(page_url, 'failed', '; '.join(str(item.get('error') or '') for item in failures[:3]))

        success_lines = [
            f"- #{item['sequence']}: {item['file_name']} ({item['method']})"
            for item in successes
        ]
        failure_lines = [
            f"- #{item['sequence']}: {item['image_url']} -> {item['error']}"
            for item in failures
        ]
        msg = (
            f'✅ 当前藏品批量下载完成: 成功 {len(successes)} 张，失败 {len(failures)} 张\n'
            f'{sequence_note + chr(10) if sequence_note else ""}'
            f'- 页面 URL: {page_url}\n'
            f'- 提取到候选 URL: {len(image_urls)} 个\n'
            f'- 下一安全序号: {next_sequence}\n'
            f'- 成功列表:\n' + ('\n'.join(success_lines) if success_lines else '无') + '\n'
            f'- 失败列表:\n' + ('\n'.join(failure_lines) if failure_lines else '无')
        )
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'当前藏品批量下载成功 {len(successes)} 张，下一序号 {next_sequence}',
            attachments=[str(AGENT_DATA_DIR / _safe_agent_data_filename(params.record_filename, 'image_record.jsonl'))],
        )
    except Exception as e:
        return ActionResult(error=f'批量下载当前 Kyohaku 藏品图片时出错: {str(e)}')


@legacy_tools_action(
    description=(
        'Kyohaku 图片浏览器会话保存工具：作为“右键另存为”的自动化等价方案。'
        '它不操作不稳定的系统右键菜单，而是在当前浏览器页面上下文中 fetch 图片 Blob，'
        '使用浏览器会话/Cookie 获取图片字节，保存到默认 image 目录，并同步写入记录文件。'
        '当 download_kyohaku_image 的 Python 直连失败时优先调用；再失败才截图。'
    ),
    param_model=SaveKyohakuImageViaBrowserParams,
)
async def save_kyohaku_image_via_browser(params: SaveKyohakuImageViaBrowserParams, browser_session):
    """
    使用浏览器页面上下文保存图片，等价于借助浏览器当前会话执行“另存为”。
    """
    try:
        page_data = await _extract_kyohaku_image_candidates(browser_session)
        page_url = params.page_url.strip() or page_data.get('page_url', '')
        try:
            image_url, candidates = _resolve_kyohaku_image_url(
                params.image_url,
                page_url,
                page_data,
                params.image_index,
            )
            _validate_kyohaku_image_url(image_url)
        except Exception as e:
            return ActionResult(error=str(e))

        file_prefix = _prefix_from_filename(params.file_name, 'temple')
        sequence, sequence_note = _safe_requested_image_sequence(params.sequence, params.record_filename, file_prefix)
        file_name = _numbered_file_stem(params.file_name, sequence, file_prefix)
        title = _renumber_title_if_needed(params.title, sequence)

        image_path = await _browser_fetch_image_to_file(browser_session, image_url, file_name)
        record_result = await _record_saved_kyohaku_image(
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
        )
        if record_result.error:
            return record_result

        strategy_after = _record_kyohaku_method_success('browser_context_fetch', sequence, image_url)
        _update_kyohaku_queue_status(page_url, 'downloaded')
        msg = (
            f'✅ 已通过浏览器会话保存 Kyohaku 图片 #{sequence}: {image_path.name}\n'
            f'{sequence_note + chr(10) if sequence_note else ""}'
            f'- 等价动作: 当前浏览器会话获取图片字节（替代右键另存为）\n'
            f'- 原图 URL: {image_url}\n'
            f'- 页面 URL: {page_url}\n'
            f'- 文件大小: {image_path.stat().st_size} bytes\n'
            f'- DOM 候选数: {len(candidates)}\n'
            f'- 方法策略: 当前方法 browser_context_fetch 连续成功 {strategy_after.get("streak", 0)} 次'
            f'{f"，已锁定后续优先使用 {strategy_after.get("preferred_method")}" if strategy_after.get("preferred_method") else ""}\n'
            f'{record_result.extracted_content or ""}'
        )
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'已通过浏览器会话保存并记录 Kyohaku 图片 #{sequence}: {image_path.name}',
            attachments=[str(image_path)],
        )
    except Exception as e:
        return ActionResult(error=f'浏览器会话保存 Kyohaku 图片时出错: {str(e)}')


@legacy_tools_action(
    description=(
        'Kyohaku 截图兜底工具：先打开 /art_images/ 原图页（如果提供 image_url），'
        '能直接获取原始图片字节时优先保存原始格式；否则只截取页面中最大的可见 <img> 元素，'
        '自动裁掉黑色或白色背景/边框，并尽量沿用原图扩展名高质量重编码，'
        '最后同步写入 image_record.jsonl、title.txt 和 temple_photo_info.md。'
    ),
    param_model=CleanKyohakuScreenshotParams,
)
async def clean_kyohaku_screenshot(params: CleanKyohakuScreenshotParams, browser_session):
    """
    在原图页做精确截图；能保存原始字节时不截图，必须截图时避免把黑色/白色查看器背景或边框截进去。
    """
    try:
        native_download_error = ''
        image_url = params.image_url.strip()
        file_prefix = _prefix_from_filename(params.file_name, 'temple')
        sequence, sequence_note = _safe_requested_image_sequence(params.sequence, params.record_filename, file_prefix)
        file_name = _numbered_file_stem(params.file_name, sequence, file_prefix)
        title = _renumber_title_if_needed(params.title, sequence)
        if image_url:
            image_url = urljoin(params.page_url.strip() or '', image_url)
            parsed_host = urlparse(image_url).netloc.lower()
            if not parsed_host.endswith('kyohaku.go.jp'):
                return ActionResult(error=f'拒绝打开非 Kyohaku 官方图片 URL: {image_url}')
            await _navigate_to_image_url(browser_session, image_url)

            if params.prefer_native_download:
                try:
                    cookie_header = await _get_browser_cookie_header(browser_session, [image_url, params.page_url.strip()])
                    image_path = await _download_image_to_file(
                        image_url,
                        file_name,
                        timeout_seconds=180,
                        referer=params.page_url.strip() or image_url,
                        cookies=cookie_header,
                    )
                    record_result = await record_downloaded_image(params=RecordDownloadedImageParams(
                        sequence=sequence,
                        file_name=image_path.name,
                        title=title,
                        collection_title=params.collection_title,
                        page_url=params.page_url.strip() or image_url,
                        image_url=image_url,
                        evidence=params.evidence,
                        metadata=params.metadata,
                        summary=params.summary,
                        record_filename=params.record_filename,
                        info_filename=params.info_filename,
                    ))
                    if record_result.error:
                        return record_result

                    strategy_after = _record_kyohaku_method_success('python_direct', sequence, image_url)
                    _update_kyohaku_queue_status(params.page_url.strip() or image_url, 'downloaded')
                    msg = (
                        f'✅ 已保存原始图片字节 #{sequence}: {image_path.name}\n'
                        f'{sequence_note + chr(10) if sequence_note else ""}'
                        f'- 原图 URL: {image_url}\n'
                        f'- 文件大小: {image_path.stat().st_size} bytes\n'
                        f'- 未重新截图或重编码\n'
                        f'- 方法策略: 当前方法 python_direct 连续成功 {strategy_after.get("streak", 0)} 次'
                        f'{f"，已锁定后续优先使用 {strategy_after.get("preferred_method")}" if strategy_after.get("preferred_method") else ""}\n'
                        f'{record_result.extracted_content or ""}'
                    )
                    return ActionResult(
                        extracted_content=msg,
                        include_in_memory=True,
                        long_term_memory=f'已保存 Kyohaku 原始图片字节 #{sequence}: {image_path.name}',
                        attachments=[str(image_path)],
                    )
                except Exception as e:
                    native_download_error = str(e)
                    _record_kyohaku_method_failure('python_direct', sequence, image_url, native_download_error)

        preferred_ext = _image_suffix_from_url(image_url) if params.preserve_source_format else '.png'
        border_ratio = _normalize_border_ratio(params.border_ratio)
        image_path, rect_data = await _save_clean_visible_image_screenshot(
            browser_session,
            file_name,
            params.black_threshold,
            params.white_threshold,
            border_ratio,
            preferred_ext,
        )
        current_page_url = rect_data.get('page_url', '')
        record_page_url = params.page_url.strip() or current_page_url
        record_image_url = image_url or current_page_url

        record_result = await record_downloaded_image(params=RecordDownloadedImageParams(
            sequence=sequence,
            file_name=image_path.name,
            title=title,
            collection_title=params.collection_title,
            page_url=record_page_url,
            image_url=record_image_url,
            evidence=params.evidence,
            metadata=params.metadata,
            summary=params.summary,
            record_filename=params.record_filename,
            info_filename=params.info_filename,
        ))
        if record_result.error:
            return record_result

        strategy_after = _record_kyohaku_method_success('clean_screenshot', sequence, record_image_url)
        _update_kyohaku_queue_status(record_page_url, 'downloaded')
        image_rect = rect_data.get('image', {})
        fallback_note = f'- 原始字节保存失败，已回退截图: {native_download_error}\n' if native_download_error else ''
        msg = (
            f'✅ 已完成无黑白边原图页截图 #{sequence}: {image_path.name}\n'
            f'{sequence_note + chr(10) if sequence_note else ""}'
            f'- 当前页面: {current_page_url}\n'
            f'- 图片 URL: {record_image_url}\n'
            f'- 输出格式: {image_path.suffix}\n'
            f'- 裁剪区域: x={image_rect.get("x")}, y={image_rect.get("y")}, '
            f'w={image_rect.get("width")}, h={image_rect.get("height")}\n'
            f'- 保存大小: {image_path.stat().st_size} bytes\n'
            f'- 方法策略: 当前方法 clean_screenshot 连续成功 {strategy_after.get("streak", 0)} 次'
            f'{f"，已锁定后续优先使用 {strategy_after.get("preferred_method")}" if strategy_after.get("preferred_method") else ""}\n'
            f'{fallback_note}'
            f'{record_result.extracted_content or ""}'
        )
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'已在原图页精确截图并记录 Kyohaku 图片 #{sequence}: {image_path.name}',
            attachments=[str(image_path)],
        )
    except Exception as e:
        return ActionResult(error=f'Kyohaku 无黑白边截图时出错: {str(e)}')


class SelectDownloadFormatParams(BaseModel):
    """选择下载格式的参数模型"""
    preferred_format: str = "TIFF"  # 优先选择的格式: TIFF, JPEG, GIF, JP2, JPEG2000
    fallback_formats: list[str] = Field(
        default_factory=list,
        description='当 preferred_format 不可用时依次尝试的备选格式列表；默认不回退',
    )
    image_title: str | None = Field(
        default=None,
        description='当前图片标题；下载按钮成功点击后会立即追加写入 browseruse_agent_data/title.txt',
    )
    write_title_on_success: bool = Field(
        default=True,
        description='下载成功后是否立即记录标题到 title.txt',
    )
    direct_download: bool = Field(
        default=True,
        description='找到 TIFF URL 后是否由 Python 直接下载，避免浏览器下载状态采集卡死',
    )
    download_timeout_seconds: int = Field(
        default=180,
        ge=30,
        le=900,
        description='Python 直接下载单个文件的超时时间',
    )


@legacy_tools_action(
    description='在LOC网站详情页中自动找到 TIFF 下载 URL；默认用 Python 直接下载到 image 目录，成功后写 title.txt 和 download_record.jsonl。',
    param_model=SelectDownloadFormatParams,
)
async def select_download_format(params: SelectDownloadFormatParams, browser_session):
    """
    通过JavaScript直接操作LOC网站的下载格式<select>元素。

    工作流程：
    1. 在页面中查找 id 以 'select-resource' 开头的 <select> 元素
    2. 读取所有 <option>，通过 data-file-download 属性匹配目标格式
    3. 设置 selectedIndex 并触发 change 事件
    4. 默认用 Python 直接下载文件，避免浏览器下载 tab / .crdownload 影响状态采集
    5. 下载成功后立即把图片标题写入 browseruse_agent_data/title.txt

    参数：
    - preferred_format: 优先选择的格式（默认 TIFF），不区分大小写
    - fallback_formats: preferred_format 不可用时依次尝试的备选格式
    - image_title: 当前图片标题；为空时自动使用 document.title
    - write_title_on_success: 下载成功后是否写入 title.txt
    - direct_download: 是否用 Python 直接下载
    """
    try:
        preferred = params.preferred_format.upper().strip()
        image_title = params.image_title or ''
        fallback_formats = []
        for format_name in params.fallback_formats:
            normalized = format_name.upper().strip()
            if normalized and normalized != preferred and normalized not in fallback_formats:
                fallback_formats.append(normalized)
        requested_formats = [preferred, *fallback_formats]

        js_code = '''
        (function() {
            try {
                // 查找下载格式的 select 元素（id 以 select-resource 开头）
                const selects = document.querySelectorAll('select[id^="select-resource"]');
                if (selects.length === 0) {
                    return {
                        success: false,
                        error: "页面中未找到下载格式选择器(select-resource)",
                        page_url: window.location.href,
                        page_title: document.title,
                        available_formats: []
                    };
                }

                const select = selects[0];
                const options = Array.from(select.options);
                const requestedFormats = ''' + json_module.dumps(requested_formats, ensure_ascii=False) + ''';
                const providedTitle = ''' + json_module.dumps(image_title, ensure_ascii=False) + ''';
                const directDownload = ''' + json_module.dumps(params.direct_download) + ''';
                const normalize = (value) => String(value || '').replace(/\\u00a0/g, ' ').trim().toUpperCase();
                const cleanTitle = (value) => String(value || '')
                    .replace(/\\s+/g, ' ')
                    .replace(/\\s*\\|\\s*Library of Congress\\s*$/i, '')
                    .trim();

                // 收集所有可用格式信息
                const formats = options.map((opt, idx) => ({
                    index: idx,
                    format: opt.getAttribute('data-file-download') || '',
                    text: opt.textContent.replace(/\\u00a0/g, ' ').trim(),
                    value: opt.value
                }));

                // 优先按 requestedFormats 顺序匹配，单个格式内仍优先选择最后一个（通常尺寸最大）
                let targetIdx = -1;
                let selectedRequestedFormat = '';
                for (const requestedFormat of requestedFormats) {
                    for (let i = 0; i < options.length; i++) {
                        const fmt = normalize(options[i].getAttribute('data-file-download'));
                        const text = normalize(options[i].textContent);
                        if (fmt === requestedFormat || text.includes(requestedFormat)) {
                            targetIdx = i;
                            selectedRequestedFormat = requestedFormat;
                        }
                    }
                    if (targetIdx !== -1) {
                        break;
                    }
                }

                if (targetIdx === -1) {
                    const requestedLabel = requestedFormats.join(', ');
                    return {
                        success: false,
                        error: requestedFormats.length === 1
                            ? "未找到格式: " + requestedFormats[0]
                            : "未找到任何可用格式: " + requestedLabel,
                        requested_formats: requestedFormats,
                        page_url: window.location.href,
                        available_formats: formats,
                    };
                }

                // 选中目标选项
                select.selectedIndex = targetIdx;
                select.dispatchEvent(new Event('change', { bubbles: true }));

                // 查找 Go 按钮；直接下载模式下不点击，避免打开下载 tab
                const container = select.closest('.input-group-small') || select.parentElement;
                let goButton = container ? container.querySelector('button') : null;
                if (!goButton) {
                    goButton = document.querySelector('button.button-default');
                }

                let clicked = false;
                if (goButton && !directDownload) {
                    goButton.click();
                    clicked = true;
                }

                const rawUrl = formats[targetIdx].value;

                return {
                    success: true,
                    selected_format: formats[targetIdx].format,
                    selected_text: formats[targetIdx].text,
                    selected_requested_format: selectedRequestedFormat,
                    download_url: rawUrl ? new URL(rawUrl, window.location.href).href : '',
                    page_url: window.location.href,
                    page_title: cleanTitle(providedTitle) || cleanTitle(document.title) || window.location.href,
                    go_clicked: clicked,
                    requested_formats: requestedFormats,
                    used_fallback: selectedRequestedFormat !== requestedFormats[0],
                    available_formats: formats
                };
            } catch (error) {
                return { success: false, error: error.message };
            }
        })()
        '''

        cdp_session = await browser_session.get_or_create_cdp_session()
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
            session_id=cdp_session.session_id,
        )

        if result.get('exceptionDetails'):
            error_text = result['exceptionDetails'].get('text', '未知JS错误')
            return ActionResult(error=f'JavaScript执行失败: {error_text}')

        data = result.get('result', {}).get('value')
        if not data:
            return ActionResult(error='未获取到返回数据')

        if not data.get('success'):
            formats = data.get('available_formats', [])
            fmt_list = ', '.join(f['format'] + '(' + f['text'] + ')' for f in formats if f['format'])
            error_msg = data.get('error', '未知错误')
            _update_result_queue_status(data.get('page_url', ''), 'skipped', error_msg)
            _record_download_status({
                'title': params.image_title or data.get('page_url', ''),
                'url': data.get('page_url', ''),
                'status': 'skipped',
                'error': error_msg,
            })
            return ActionResult(
                error=f'{error_msg}。可用格式: {fmt_list}' if fmt_list else error_msg
            )

        selected = data.get('selected_text', '')
        fmt = data.get('selected_format', '')
        clicked = data.get('go_clicked', False)
        selected_requested_format = data.get('selected_requested_format', preferred)
        used_fallback = data.get('used_fallback', False)
        download_url = data.get('download_url', '')
        page_title = data.get('page_title', '') or params.image_title or download_url
        page_url = data.get('page_url', '')
        title_file_path = None
        downloaded_file_path = None
        record_file_path = None

        if params.direct_download:
            if not download_url:
                _record_download_status({
                    'title': page_title,
                    'url': download_url,
                    'status': 'failed',
                    'error': '缺少下载 URL',
                })
                _update_result_queue_status(page_url, 'failed', '缺少下载 URL')
                return ActionResult(error='已找到格式但缺少下载 URL，无法直接下载')

            async with DOWNLOAD_LOCK:
                output_dir = IMAGE_DIR
                try:
                    cookie_header = await _get_browser_cookie_header(browser_session, [download_url, page_url])
                    downloaded_file_path = await _download_file(
                        download_url,
                        page_title,
                        output_dir=output_dir,
                        timeout_seconds=params.download_timeout_seconds,
                        referer=page_url,
                        cookies=cookie_header,
                    )
                    download_method = 'python'
                except Exception as direct_download_error:
                    try:
                        before_files = _current_tiff_files(output_dir)
                        await _click_selected_download_button(browser_session)
                        fallback_timeout = max(
                            params.download_timeout_seconds,
                            int(os.environ.get('BROWSER_USE_BROWSER_FALLBACK_TIMEOUT_SECONDS', '180')),
                        )
                        downloaded_file_path = await _wait_for_browser_tiff_download(
                            output_dir,
                            before_files,
                            fallback_timeout,
                        )
                        download_method = 'browser_fallback'
                    except Exception as browser_download_error:
                        combined_error = (
                            f'Python直连失败: {direct_download_error}; '
                            f'浏览器兜底失败: {browser_download_error}'
                        )
                        record_file_path = _record_download_status({
                            'title': page_title,
                            'url': download_url,
                            'page_url': page_url,
                            'status': 'failed',
                            'error': combined_error,
                        })
                        _update_result_queue_status(page_url, 'failed', combined_error)
                        return ActionResult(error=f'直接下载 TIFF 失败: {combined_error}')

                try:
                    record_file_path = _record_download_status({
                        'title': page_title,
                        'url': download_url,
                        'page_url': page_url,
                        'status': 'downloaded',
                        'method': download_method,
                        'file_path': str(downloaded_file_path),
                        'file_size': downloaded_file_path.stat().st_size,
                    })
                    _update_result_queue_status(page_url, 'downloaded')
                except OSError as record_error:
                    return ActionResult(error=f'TIFF 已下载但记录状态失败: {record_error}')

            if params.write_title_on_success and downloaded_file_path:
                title_file_path = _append_download_title(page_title)
        elif params.write_title_on_success and clicked and download_url:
            title_file_path = _append_download_title(page_title)
            record_file_path = _record_download_status({
                'title': page_title,
                'url': download_url,
                'page_url': page_url,
                'status': 'clicked',
            })
            _update_result_queue_status(page_url, 'clicked')

        msg = f"✅ 已选择下载格式: {fmt} ({selected})"
        if used_fallback:
            msg += f"；首选 {preferred} 不可用，已自动回退到 {selected_requested_format}"
        if params.direct_download and downloaded_file_path:
            if download_method == 'browser_fallback':
                msg += f"，Python 直连失败后已由浏览器兜底下载到 {downloaded_file_path}"
            else:
                msg += f"，已由 Python 直接下载到 {downloaded_file_path}"
            if title_file_path:
                msg += f"，标题已写入 {title_file_path}"
            if record_file_path:
                msg += f"，下载记录已写入 {record_file_path}"
        elif clicked:
            msg += "，并已点击Go按钮开始下载"
            if title_file_path:
                msg += f"，标题已写入 {title_file_path}"
        else:
            msg += "，但未找到Go按钮，请手动点击"

        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'已选择{fmt}格式下载: {selected}；文件: {downloaded_file_path or "浏览器下载"}；标题记录: {title_file_path or "未写入"}',
        )

    except Exception as e:
        return ActionResult(error=f'选择下载格式时出错: {str(e)}')
