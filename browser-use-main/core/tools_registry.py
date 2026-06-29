"""
工具注册模块 - 自定义 browser-use Agent 工具

将工具定义从 main.py 中提取出来,便于管理和复用.
包含:
- Pydantic 参数模型
- Tools 实例和注册
- extract_page_to_markdown 自定义 action
"""

import asyncio
import base64
import hashlib
import ipaddress
import json as json_module
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
import anyio
from pydantic import BaseModel, Field

from adapters.idp import IDPAdapter
from browser_use import ActionResult, Tools
from concurrent_download import ConcurrentImageDownloader, image_download_concurrency
from batch_download import run_search_page_batch
from idp_page_progress import load_page_progress, mark_page_batch_result

# 使用工程根目录作为项目基准路径(本模块位于 core/ 下,需回退一级);运行产物目录可由 worker.py 动态配置
PROJECT_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = PROJECT_DIR
RUN_DIR = Path(os.environ.get('BROWSER_USE_RUN_DIR', str(PROJECT_DIR))).resolve()
IMAGE_DIR = Path(os.environ.get('BROWSER_USE_IMAGE_DIR', str(PROJECT_DIR / 'image'))).resolve()
AGENT_DATA_DIR = Path(os.environ.get('BROWSER_USE_AGENT_DATA_DIR', str(PROJECT_DIR / 'browseruse_agent_data'))).resolve()
DOWNLOAD_LOCK = asyncio.Lock()

EXT_TO_PIL_FORMAT = {
    '.jpg': 'JPEG',
    '.jpeg': 'JPEG',
    '.png': 'PNG',
    '.webp': 'WEBP',
    '.bmp': 'BMP',
    '.tif': 'TIFF',
    '.tiff': 'TIFF',
}
GENERAL_IMAGE_METHODS = ('python_direct', 'browser_context_fetch', 'clean_screenshot')
GENERAL_IMAGE_STRATEGY_LOCK_THRESHOLD = 5


@dataclass
class DownloadRecordIndex:
    """In-memory indexes for one batch run, avoiding repeated JSONL scans."""

    record_file: Path
    records: list[dict] = field(default_factory=list)
    downloaded_count: int = 0
    max_sequence: int = 0
    used_sequences: set[int] = field(default_factory=set)
    records_by_image_url: dict[str, dict] = field(default_factory=dict)
    records_by_file_hash: dict[str, dict] = field(default_factory=dict)
    records_by_source_hash: dict[str, dict] = field(default_factory=dict)

    def add_record(self, record: dict) -> None:
        self.records.append(record)
        if record.get('status') != 'downloaded':
            return

        self.downloaded_count += 1
        sequence = _record_sequence(record)
        if sequence is not None:
            self.used_sequences.add(sequence)
            self.max_sequence = max(self.max_sequence, sequence)

        image_url = str(record.get('image_url') or '').strip()
        if image_url:
            self.records_by_image_url[image_url] = record

        file_hash = _record_file_sha256(record)
        if file_hash:
            self.records_by_file_hash[file_hash] = record

        source_hash = str(record.get('source_hash') or '').strip()
        if source_hash:
            self.records_by_source_hash[source_hash] = record


# === 定义参数模型 ===

class ExtractPageContentParams(BaseModel):
    """提取网页内容的参数模型"""
    output_filename: str = "page_content.md"
    output_dir: str = str(Path(__file__).resolve().parent / "image")
    format_type: str = "markdown"  # markdown, json, text
    information_file_path: str = str(Path(__file__).resolve().parent.parent / "legacy" / "Information.md")


class WaitForHumanVerificationParams(BaseModel):
    """等待人工完成人机验证的参数模型"""
    timeout_seconds: int = Field(default=180, ge=1, le=900, description='最多等待人工完成验证的秒数')
    poll_interval_seconds: int = Field(default=5, ge=1, le=30, description='检查页面是否恢复的间隔秒数')
    auto_click: bool = Field(default=True, description='是否先尝试自动点击 Cloudflare/Turnstile 复选框;失败再回退人工等待')
    auto_click_attempts: int = Field(default=3, ge=1, le=10, description='自动点击的最大尝试轮数')


class RecordDownloadedImageParams(BaseModel):
    """记录已保存图片的参数模型"""
    sequence: int = Field(ge=1, description='图片序号,从 1 开始,应与保存文件名顺序一致')
    file_name: str = Field(description='已保存到 image 目录中的文件名,例如 temple_001.png')
    title: str = Field(description='用于最终重命名的短标题,例如 寺_001_藏品标题_图1')
    collection_title: str = Field(default='', description='藏品页面显示的原始标题')
    page_url: str = Field(default='', description='藏品详情页 URL')
    image_url: str = Field(default='', description='原始图片 URL,优先使用 /art_images/...-L.jpg')
    evidence: str = Field(default='', description='判断与关键词相关的证据')
    metadata: str = Field(default='', description='作者,时代,分类,馆藏号等信息')
    summary: str = Field(default='', description='图片或藏品的简短中文说明')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')


class DownloadImageFromUrlParams(BaseModel):
    """通用图片 URL 下载并记录的参数模型"""
    sequence: int = Field(ge=1, description='图片序号,从 1 开始;工具会自动修正为当前下一安全序号,避免覆盖')
    file_name: str = Field(default='temple_001', description='保存文件名或基础名,例如 temple_001;扩展名会优先使用图片 URL 或响应类型')
    title: str = Field(description='用于最终重命名的短标题,例如 china_temple_001_藏品标题_图1')
    collection_title: str = Field(default='', description='藏品页面显示的原始标题')
    page_url: str = Field(default='', description='藏品详情页 URL;也会作为默认 Referer')
    image_url: str = Field(default='', description='可直接访问的图片 URL,例如 IIIF /full/max/0/default.jpg,IIIF manifest URL 或 viewer 大图 URL;为空时会从当前页面自动查找候选图片 URL')
    image_index: int = Field(default=0, ge=0, description='image_url 为空时,从当前页面自动候选列表中选择第几个,按大图优先排序')
    evidence: str = Field(default='', description='判断与关键词相关的证据')
    metadata: str = Field(default='', description='作者,时代,地点,分类,馆藏号等信息')
    summary: str = Field(default='', description='图片或藏品的简短中文说明')
    referer: str = Field(default='', description='可选 Referer;为空时使用 page_url')
    use_browser_cookies: bool = Field(default=True, description='Python 直连下载时是否附带当前浏览器会话 Cookie')
    prefer_browser_fetch: bool = Field(default=False, description='是否优先使用浏览器上下文 fetch;为空策略时默认先用 Python 直连,失败后浏览器 fetch')
    allow_clean_screenshot: bool = Field(default=True, description='直连和浏览器 fetch 都失败时,是否打开图片页并精确裁剪可见图片作为兜底')
    black_threshold: int = Field(default=18, ge=0, le=80, description='截图兜底自动去黑边阈值')
    white_threshold: int = Field(default=245, ge=180, le=255, description='截图兜底自动去白边阈值')
    border_ratio: float = Field(default=0.985, description='截图兜底一整行/列超过该比例为黑色或白色时才视为边框;工具会把异常值归一化到 0.90-0.999')
    allowed_host_suffixes: list[str] = Field(default_factory=list, description='可选域名后缀白名单,例如 ["example.org", "data.example.org"];为空时允许任意公网 http(s) 图片 URL')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')
    timeout_seconds: int = Field(default=180, ge=30, le=900, description='直接下载超时时间')
    force_generic: bool = Field(default=False, description='稳定优先:跳过站点专属的 URL→IIIF manifest 推导加速,只用传入/页面识别到的图片直链 + DOM 候选 + 三级兜底,对任意站点走同一条久经验证的通用路径(牺牲效率换稳定)')


class NextSearchItemParams(BaseModel):
    """next_search_item(统一发号)工具的参数模型."""
    keyword: str = Field(default='', description='当前搜索关键词,仅用于在游标文件里标注,可留空')
    item_selector: str = Field(
        default='',
        description='搜索结果页中 item 详情链接的 CSS 选择器;留空时按当前站点已注册的 hint 自动选择(如 idp.bl.uk 注册了 a[href*="/collection/"])',
    )
    mark_done_url: str = Field(
        default='',
        description='可选:刚刚处理完(已下载或主动跳过)的那个 item 的详情页 URL;传入后会标记为已处理再发下一个',
    )
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名,用于交叉核对已下载的 item')
    max_scan: int = Field(default=500, ge=1, le=2000, description='单页最多枚举多少个 item')


class ValidateDownloadCompletionParams(BaseModel):
    """最终下载结果校验参数模型"""
    target_count: int = Field(default=100, ge=1, description='目标有效下载数量')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')


class FinishDownloadTaskParams(BaseModel):
    """用确定性校验报告结束任务的参数模型"""
    target_count: int = Field(default=100, ge=1, description='目标有效下载数量')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')


# === 创建 tools 对象 ===
tools = Tools()
registry = tools.registry


# === Legacy 工具注册开关 ===
#
# LOC(Library of Congress)和 Kyohaku(京都国立博物馆)的整套适配器在当前 IDP
# 主线任务里既不使用,也被 task.md 显式禁止;为了避免它们出现在 Agent 的工具列表
# 里干扰决策,默认不注册.
#
# 如果以后要恢复使用,设环境变量 BROWSER_USE_ENABLE_LEGACY_TOOLS=1 即可,代码本身
# 没有移动,原地仍可阅读和维护.详见 legacy/README.md.
LEGACY_TOOLS_ENABLED = os.environ.get('BROWSER_USE_ENABLE_LEGACY_TOOLS', '').strip() in {'1', 'true', 'TRUE', 'yes', 'YES'}


def legacy_tools_action(*args, **kwargs):
    """Conditional wrapper around @tools.action for LOC/Kyohaku legacy tools.

    When BROWSER_USE_ENABLE_LEGACY_TOOLS is set, behaves exactly like
    `tools.action(...)`. Otherwise the decorated function is returned untouched,
    so it stays callable from Python but is not registered into the agent's
    tool catalogue.
    """
    if LEGACY_TOOLS_ENABLED:
        return tools.action(*args, **kwargs)

    def _noop(func):
        return func

    return _noop


# === 路径安全验证 ===

# 允许访问的基础目录(基于项目位置)
ALLOWED_BASE_DIRS = [
    PROJECT_DIR,
    RUN_DIR,
    Path(os.environ.get('BROWSER_USE_DOWNLOAD_DIR', str(Path.home() / 'Downloads'))),
]


def configure_runtime_paths(run_dir: Path, image_dir: Path | None = None, data_dir: Path | None = None) -> None:
    """
    配置本次运行的图片和数据目录.main.py 会在创建 ImagesCache 后调用.
    """
    global RUN_DIR, IMAGE_DIR, AGENT_DATA_DIR
    RUN_DIR = Path(run_dir).resolve()
    IMAGE_DIR = Path(image_dir or RUN_DIR).resolve()
    AGENT_DATA_DIR = Path(data_dir or RUN_DIR).resolve()
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.environ['BROWSER_USE_RUN_DIR'] = str(RUN_DIR)
    os.environ['BROWSER_USE_IMAGE_DIR'] = str(IMAGE_DIR)
    os.environ['BROWSER_USE_AGENT_DATA_DIR'] = str(AGENT_DATA_DIR)
    for path in (RUN_DIR, IMAGE_DIR, AGENT_DATA_DIR):
        if path not in ALLOWED_BASE_DIRS:
            ALLOWED_BASE_DIRS.append(path)


def _is_path_allowed(target_path: str, allowed_bases: list[Path] = ALLOWED_BASE_DIRS) -> bool:
    """
    验证目标路径是否在允许的基础目录下,
    防止 LLM 通过工具参数读写任意文件.
    """
    try:
        resolved = Path(target_path).resolve()
        return any(
            resolved == base.resolve() or resolved.is_relative_to(base.resolve())
            for base in allowed_bases
        )
    except (ValueError, OSError):
        return False


def _resolve_extract_paths(params: "ExtractPageContentParams") -> tuple[Path, Path]:
    """
    解析并校验提取工具的输入路径,不合法时回退到项目默认路径.
    """
    default_info_path = BASE_DIR / 'legacy' / 'Information.md'
    default_output_dir = IMAGE_DIR

    raw_info_path = (params.information_file_path or '').strip()
    info_path = Path(raw_info_path) if raw_info_path else default_info_path
    if not info_path.is_absolute():
        info_path = BASE_DIR / info_path
    if not _is_path_allowed(str(info_path)) or not info_path.is_file():
        info_path = default_info_path

    raw_output_dir = (params.output_dir or '').strip()
    output_dir = Path(raw_output_dir) if raw_output_dir else default_output_dir
    if not output_dir.is_absolute():
        output_dir = BASE_DIR / output_dir
    if output_dir.resolve() == BASE_DIR.resolve():
        output_dir = default_output_dir
    if not _is_path_allowed(str(output_dir)):
        output_dir = default_output_dir

    return info_path, output_dir


def _safe_extract_filename(filename: str, file_ext: str) -> str:
    """
    为提取出的 Markdown/JSON/TXT 生成稳定安全的文件名,避免 agent 传入目录,空值或 Windows 特殊字符.
    """
    raw_name = _normalize_title(filename, fallback='page_content')
    raw_name = Path(raw_name).name
    raw_name = re.sub(r'\.(md|json|txt)$', '', raw_name, flags=re.IGNORECASE)
    safe_name = re.sub(r'[<>:"/\\|?*;{}\[\]\x00-\x1f]', '_', raw_name)
    safe_name = re.sub(r'[^\w\s().,-]', '_', safe_name, flags=re.UNICODE)
    safe_name = re.sub(r'\s+', '_', safe_name)
    safe_name = re.sub(r'_+', '_', safe_name).strip(' ._-')
    if not safe_name:
        safe_name = 'page_content'
    return f'{safe_name[:180]}{file_ext}'


def _load_information_patterns(info_file_path: Path) -> list[dict]:
    """
    读取 Information.md 并提取 HTML 代码块的首尾模式.
    """
    if not info_file_path.exists():
        raise FileNotFoundError(f'Information.md文件不存在: {info_file_path}')

    info_content = info_file_path.read_text(encoding='utf-8')
    info_content = info_content.replace('\r\n', '\n').replace('\r', '\n')

    html_blocks = re.findall(r"```html\n([\s\S]*?)```", info_content)
    if not html_blocks:
        raise ValueError('Information.md中没有找到HTML代码块')

    search_patterns = []
    for block in html_blocks:
        lines = block.strip().split('\n')
        if len(lines) >= 1:
            first_line = lines[0].strip()
            last_line = lines[-1].strip()
            if first_line and last_line:
                search_patterns.append({
                    'start': first_line,
                    'end': last_line,
                    'full_block': block,
                })

    if not search_patterns:
        raise ValueError('未能从HTML代码块中提取有效的首尾行')

    return search_patterns


def _write_extracted_file(output_dir: Path, file_name: str, file_content: str) -> Path:
    """
    把提取结果写入磁盘.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / file_name
    output_path.write_text(file_content, encoding='utf-8')
    return output_path


def _normalize_title(title: str, fallback: str = 'untitled') -> str:
    """
    清理图片标题,保证每个标题只占一行(用于文件名词干,记录字段和信息表).
    """
    normalized = title.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized or fallback


def _safe_download_filename(title: str, url: str, suffix: str = '.tif') -> str:
    """
    根据标题生成稳定,安全的下载文件名.
    """
    normalized = _normalize_title(title, fallback='untitled')
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', normalized)
    safe_name = re.sub(r'\s+', '_', safe_name).strip('._ ')
    safe_name = safe_name[:180] or 'untitled'
    parsed_suffix = Path(urlparse(url).path).suffix.lower()
    if parsed_suffix in {'.tif', '.tiff'}:
        suffix = parsed_suffix
    return f'{safe_name}{suffix}'


def _unique_path(path: Path) -> Path:
    """
    如果文件已存在,追加短 hash 避免覆盖.
    """
    if not path.exists():
        return path
    digest = hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:8]
    candidate = path.with_name(f'{path.stem}_{digest}{path.suffix}')
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f'{path.stem}_{digest}_{counter}{path.suffix}')
        counter += 1
    return candidate


def _current_tiff_files(output_dir: Path) -> dict[Path, float]:
    """
    记录当前目录中已完成的 TIFF 文件,用于识别浏览器兜底下载产生的新文件.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    return {
        path: path.stat().st_mtime
        for pattern in ('*.tif', '*.tiff')
        for path in output_dir.glob(pattern)
        if path.is_file()
    }


async def _download_file(
    url: str,
    title: str,
    output_dir: Path | None = None,
    timeout_seconds: int = 180,
    referer: str | None = None,
    cookies: str | None = None,
) -> Path:
    """
    用 Python 直接下载文件,避免浏览器下载 tab / .crdownload / watchdog 状态采集干扰.
    """
    target_dir = output_dir or IMAGE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _unique_path(target_dir / _safe_download_filename(title, url))
    tmp_path = target_path.with_suffix(target_path.suffix + '.part')

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept': 'image/tiff,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if referer:
        headers['Referer'] = referer
    if cookies:
        headers['Cookie'] = cookies
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status >= 400:
                raise RuntimeError(f'HTTP {response.status}: {url}')
            async with await anyio.open_file(tmp_path, 'wb') as f:
                async for chunk in response.content.iter_chunked(1024 * 256):
                    if chunk:
                        await f.write(chunk)

    if tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f'下载文件为空: {url}')

    tmp_path.replace(target_path)
    try:
        _validate_saved_image_file(target_path, source='python_direct')
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    return target_path


async def _get_browser_cookie_header(browser_session, urls: list[str]) -> str:
    """
    从当前浏览器会话提取相关 URL 的 Cookie,供 Python 直接下载使用.
    """
    try:
        cdp_session = await browser_session.get_or_create_cdp_session()
        result = await cdp_session.cdp_client.send.Network.getCookies(
            params={'urls': [url for url in urls if url]},
            session_id=cdp_session.session_id,
        )
        cookies = result.get('cookies', [])
        return '; '.join(
            f"{cookie['name']}={cookie['value']}"
            for cookie in cookies
            if cookie.get('name') and cookie.get('value') is not None
        )
    except Exception:
        return ''


def _load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json_module.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except json_module.JSONDecodeError:
        return []


def _load_jsonl_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json_module.loads(line)
        except json_module.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _image_record_file(record_filename: str = 'image_record.jsonl') -> Path:
    """
    获取图片记录文件路径,并限制文件名只能落在 browseruse_agent_data 下.
    """
    normalized = re.sub(r'\s+', '', str(record_filename or '').strip())
    safe_name = Path(normalized).name
    if safe_name in {'', '.', 'image_record'} or not safe_name.endswith('.jsonl'):
        safe_name = 'image_record.jsonl'
    return AGENT_DATA_DIR / safe_name


def _max_downloaded_record_sequence(record_filename: str = 'image_record.jsonl') -> int:
    """
    返回已成功下载记录中的最大序号,避免 agent 传错 start_sequence 后覆盖旧图.
    """
    max_sequence = 0
    for record in _load_image_records(_image_record_file(record_filename)):
        if record.get('status') != 'downloaded':
            continue
        try:
            max_sequence = max(max_sequence, int(record.get('sequence') or 0))
        except (TypeError, ValueError):
            continue
    return max_sequence


def _max_image_file_sequence(file_prefix: str = 'temple') -> int:
    """
    返回 image 目录中同前缀文件名的最大数字序号.
    """
    prefix = re.escape((file_prefix or 'temple').strip() or 'temple')
    pattern = re.compile(rf'^{prefix}_(\d+)(?:[_-].*)?$', re.IGNORECASE)
    max_sequence = 0
    if not IMAGE_DIR.exists():
        return 0
    for path in IMAGE_DIR.iterdir():
        if not path.is_file():
            continue
        match = pattern.match(path.stem)
        if not match:
            continue
        try:
            max_sequence = max(max_sequence, int(match.group(1)))
        except ValueError:
            continue
    return max_sequence


def _sequence_from_filename(file_name: str) -> int | None:
    match = re.search(r'_(\d{3,})(?:\D|$)', Path(file_name or '').stem)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _prefix_from_filename(file_name: str, default: str = 'temple') -> str:
    stem = Path(file_name or '').stem
    match = re.match(r'^(.+?)_\d{3,}(?:\D.*)?$', stem)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return default


def _numbered_file_stem(file_name: str, sequence: int, default_prefix: str = 'temple') -> str:
    """
    把文件名调整为 prefix_###,避免重复使用 agent 传入的旧序号.
    """
    stem = Path(file_name or '').stem.strip()
    prefix = _prefix_from_filename(stem, default_prefix)
    return f'{prefix}_{sequence:03d}'


def _next_available_image_sequence(record_filename: str = 'image_record.jsonl', file_prefix: str = 'temple') -> int:
    return max(_max_downloaded_record_sequence(record_filename), _max_image_file_sequence(file_prefix)) + 1


def _safe_requested_image_sequence(
    requested_sequence: int,
    record_filename: str = 'image_record.jsonl',
    file_prefix: str = 'temple',
) -> tuple[int, str]:
    """
    如果 agent 传入的序号已经用过或会覆盖文件,自动提升到安全序号.
    """
    next_sequence = _next_available_image_sequence(record_filename, file_prefix)
    if requested_sequence != next_sequence:
        return next_sequence, f'⚠️ agent 传入序号 {requested_sequence} 与当前下一安全序号不一致，已自动改为 {next_sequence}'
    return next_sequence, ''


def _safe_record_sequence_for_existing_file(
    requested_sequence: int,
    record_filename: str,
    file_prefix: str,
    image_path: Path,
) -> tuple[int, str]:
    """
    record_downloaded_image 接收的是已落地的临时文件.若临时文件本身就是本次请求序号
    (如 temple_001.jpg),不能再把它算作“已占用”而跳到 002.
    """
    records = _load_image_records(_image_record_file(record_filename))
    used_sequences = {
        sequence
        for sequence in (_record_sequence(record) for record in records if record.get('status') == 'downloaded')
        if sequence is not None
    }
    if requested_sequence in used_sequences:
        return _safe_requested_image_sequence(requested_sequence, record_filename, file_prefix)

    file_sequence = _sequence_from_filename(image_path.name)
    if file_sequence == requested_sequence:
        return requested_sequence, ''

    return _safe_requested_image_sequence(requested_sequence, record_filename, file_prefix)


def _renumber_title_if_needed(title: str, sequence: int) -> str:
    if not title:
        return title
    return re.sub(r'_(\d{1,4})(?=_)', f'_{sequence:03d}', title, count=1)


def _normalize_border_ratio(border_ratio: float) -> float:
    try:
        value = float(border_ratio)
    except (TypeError, ValueError):
        return 0.985
    if value < 0.90:
        return 0.90
    if value > 0.999:
        return 0.999
    return value


def _coerce_int(value, default: int, minimum: int, maximum: int) -> int:
    text = str(value if value is not None else '').strip()
    match = re.search(r'\d+', text)
    try:
        number = int(match.group(0) if match else text)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _clean_url_text(url: str) -> str:
    cleaned = str(url or '').strip()
    cleaned = cleaned.strip('`"\' \t\r\n')
    cleaned = re.sub(r'[\]\)},,.;;]+$', '', cleaned)
    return cleaned


def _sanitize_allowed_host_suffixes(suffixes: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in suffixes or []:
        text = str(raw or '').strip().strip('[](){}"\'`')
        for part in re.split(r'[,,\s]+', text):
            host = part.strip().strip('[]"\'`.').lower()
            if not host or '/' in host or ':' in host:
                continue
            if re.fullmatch(r'[a-z0-9.-]+', host) and host not in cleaned:
                cleaned.append(host)
    return cleaned


def _choose_reliable_page_url(agent_page_url: str, current_page_url: str) -> tuple[str, str]:
    agent_url = _clean_url_text(agent_page_url)
    current_url = _clean_url_text(current_page_url)
    if _site_invalid_collection_url(agent_url):
        if _site_valid_page_url(current_url):
            return current_url, f'- 已忽略模型传入的非法详情页 URL，改用当前页面: {current_url}\n'
        return '', f'- 已拒绝模型传入的非法详情页 URL（疑似搜索/列表页）: {agent_url}\n'
    return agent_url or current_url, ''


def _write_json_list(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_module.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')


def _safe_agent_data_filename(filename: str, fallback: str) -> str:
    """
    限制 agent 数据文件名只能落在 browseruse_agent_data 根目录,避免目录穿越.
    """
    normalized = re.sub(r'\s+', '', str(filename or fallback).strip())
    safe_name = Path(normalized or fallback).name
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', safe_name).strip(' ._')
    return safe_name or fallback


def _load_image_records(record_file: Path) -> list[dict]:
    if not record_file.exists():
        return []

    records: list[dict] = []
    for line in record_file.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json_module.loads(line)
        except json_module.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _write_image_records(record_file: Path, records: list[dict]) -> None:
    record_file.parent.mkdir(parents=True, exist_ok=True)
    content = ''.join(json_module.dumps(record, ensure_ascii=False) + '\n' for record in records)
    record_file.write_text(content, encoding='utf-8')


def _find_downloaded_record_by_image_url(record_filename: str, image_url: str) -> dict | None:
    normalized_url = (image_url or '').strip()
    if not normalized_url:
        return None
    for record in _load_image_records(_image_record_file(record_filename)):
        if record.get('status') == 'downloaded' and str(record.get('image_url') or '').strip() == normalized_url:
            return record
    return None


def _record_sort_key(record: dict) -> tuple[int, str]:
    sequence = record.get('sequence')
    if not isinstance(sequence, int):
        try:
            sequence = int(sequence)
        except (TypeError, ValueError):
            sequence = 10**9
    return sequence, str(record.get('file_name') or '')


def _record_sequence(record: dict) -> int | None:
    try:
        return int(record.get('sequence'))
    except (TypeError, ValueError):
        return None


def _markdown_cell(value: object) -> str:
    """
    转义 Markdown 表格单元格,保持记录文件可解析.
    """
    text = str(value or '').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    text = text.replace('|', '\\|')
    return re.sub(r'\s+', ' ', text).strip()


def _rewrite_image_info_file(data_dir: Path, records: list[dict], info_filename: str) -> Path:
    info_file = data_dir / _safe_agent_data_filename(info_filename, 'temple_photo_info.md')
    lines = [
        '# 图片下载记录',
        '',
        '| 序号 | 保存文件名 | 重命名标题 | 藏品标题 | 藏品 URL | 图片 URL | 相关证据 | 作者/时代/分类/馆藏号 | 简短说明 |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    for record in sorted(records, key=_record_sort_key):
        if record.get('status') != 'downloaded':
            continue
        lines.append(
            '| '
            + ' | '.join(
                [
                    _markdown_cell(record.get('sequence')),
                    _markdown_cell(record.get('file_name')),
                    _markdown_cell(record.get('title')),
                    _markdown_cell(record.get('collection_title')),
                    _markdown_cell(record.get('page_url')),
                    _markdown_cell(record.get('image_url')),
                    _markdown_cell(record.get('evidence')),
                    _markdown_cell(record.get('metadata')),
                    _markdown_cell(record.get('summary')),
                ]
            )
            + ' |'
        )
    info_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return info_file


def _record_image_file_path(file_name: str) -> Path:
    """
    将保存文件名解析到项目 image 目录;只接受文件名,禁止目录穿越.
    """
    safe_name = Path(file_name).name
    return IMAGE_DIR / safe_name


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_text(value: str, algorithm: str = 'sha256') -> str:
    digest = hashlib.new(algorithm)
    digest.update(value.encode('utf-8'))
    return digest.hexdigest()


def _normalize_source_url(url: str) -> str:
    cleaned = _clean_url_text(url)
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        return cleaned
    return parsed._replace(fragment='', netloc=parsed.netloc.lower()).geturl().rstrip('/')


def _source_hash(page_url: str, image_url: str, image_index: int = 0) -> str:
    source_key = '|'.join([
        _normalize_source_url(page_url),
        _normalize_source_url(image_url),
        str(image_index),
    ])
    return _hash_text(source_key, 'sha256')


def _source_item_id_from_urls(page_url: str, image_url: str = '') -> str:
    """从 URL 提取站内 item id:优先用站点 hint,无则用通用兜底(URL 路径末段)."""
    site_id = _site_item_id_from_urls(page_url, image_url)
    if site_id:
        return site_id
    # 通用兜底:取详情页 URL 路径里最后一个非空,非纯数字的路径段作为 item id,
    # 对任意站点都给出一个稳定可读的标识(牺牲站点精度换全站点可用).
    for raw_url in (page_url, image_url):
        parsed = urlparse((raw_url or '').strip())
        path_parts = [part for part in parsed.path.split('/') if part]
        for segment in reversed(path_parts):
            seg = segment.strip()
            if seg and not seg.isdigit() and '.' not in seg:
                return seg.upper()
    return ''


def _titled_image_stem(title: str, sequence: int) -> str:
    """
    生成"序号_标题"形式的可读文件名词干(不含 hash 后缀,不含扩展名).

    既用于下载落地时的临时名(落地即可读:以图片自己的 title 命名),
    也用于最终名的前缀部分;最终名再在其后追加信息 hash.两处共用同一词干,
    保证临时名与最终名的可读前缀完全一致.
    """
    normalized_title = _normalize_title(title, fallback=f'image_{sequence:03d}')
    normalized_title = re.sub(r'^(?:temple|image)_\d{1,6}_?', '', normalized_title, flags=re.IGNORECASE)
    if not re.search(r'_\d{3,}(?:_|$)', normalized_title):
        normalized_title = f'{sequence:03d}_{normalized_title}'
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', normalized_title)
    safe_stem = re.sub(r'_+', '_', safe_stem).strip('._ ')[:180] or f'image_{sequence:03d}'
    return safe_stem


def _final_image_filename(title: str, sequence: int, embed_hash: str, suffix: str) -> str:
    """
    最终文件名 = 序号_标题_信息hash8位.ext.

    ``embed_hash`` 是要嵌入文件名的指纹,调用方传入该图片"对应信息"的 source_hash
    (sha256(page_url|image_url|index)),使文件名自带信息绑定指纹,海量数据下也能
    把图片与其信息一一对应,不串位.
    """
    safe_stem = _titled_image_stem(title, sequence)
    short_hash = (embed_hash or '')[:8] or 'nohash'
    if not safe_stem.endswith(f'_{short_hash}'):
        safe_stem = f'{safe_stem}_{short_hash}'
    return f'{safe_stem}{normalize_image_ext(suffix, fallback=".jpg")}'


def _rename_image_to_final_name(image_path: Path, title: str, sequence: int, embed_hash: str) -> Path:
    final_name = _final_image_filename(title, sequence, embed_hash, image_path.suffix)
    final_path = image_path.with_name(final_name)
    if image_path.resolve() == final_path.resolve():
        return image_path.resolve()
    final_path = _unique_path(final_path)
    image_path.rename(final_path)
    return final_path.resolve()


def _record_file_sha256(record: dict) -> str:
    recorded_hash = str(record.get('sha256') or record.get('file_sha256') or '').strip().lower()
    if re.fullmatch(r'[0-9a-f]{64}', recorded_hash):
        return recorded_hash
    file_name = Path(str(record.get('file_name') or '')).name
    if not file_name:
        return ''
    image_path = _record_image_file_path(file_name)
    if not image_path.exists() or not image_path.is_file():
        return ''
    try:
        return _sha256_file(image_path)
    except OSError:
        return ''


def _find_downloaded_record_by_file_hash(record_filename: str, file_hash: str) -> dict | None:
    normalized_hash = (file_hash or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', normalized_hash):
        return None
    for record in _load_image_records(_image_record_file(record_filename)):
        if record.get('status') == 'downloaded' and _record_file_sha256(record) == normalized_hash:
            return record
    return None


def _find_existing_image_file_by_hash(file_hash: str, exclude_path: Path | None = None) -> Path | None:
    normalized_hash = (file_hash or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', normalized_hash) or not IMAGE_DIR.exists():
        return None
    excluded = exclude_path.resolve() if exclude_path else None
    for image_path in sorted(IMAGE_DIR.iterdir(), key=lambda path: path.name.lower()):
        if not image_path.is_file() or image_path.name == 'rename_record.txt':
            continue
        if normalize_image_ext(image_path.suffix, fallback='') not in EXT_TO_PIL_FORMAT:
            continue
        try:
            if excluded and image_path.resolve() == excluded:
                continue
            if _sha256_file(image_path) == normalized_hash:
                return image_path
        except OSError:
            continue
    return None


def _build_download_record_index(record_filename: str = 'image_record.jsonl') -> DownloadRecordIndex:
    """
    Load image_record.jsonl once and build O(1) lookup indexes for a batch.
    """
    record_file = _image_record_file(record_filename)
    index = DownloadRecordIndex(record_file=record_file)
    for record in _load_image_records(record_file):
        index.add_record(record)
    return index


def _build_existing_image_hash_index(record_index: 'DownloadRecordIndex | None' = None) -> dict[str, Path]:
    """
    扫描 IMAGE_DIR 建立 sha256 -> Path 索引,供本批次去重使用.

    现实里每次新批次都重新 sha256 整个 image 目录会让"下载越多越慢"--
    一千张 TIFF 起步就是几个 GB 的 IO.优化:优先复用 image_record.jsonl
    里已经存好的 sha256(O(1) 取值),只对没有对应记录的孤儿文件再走真正
    的 sha256 计算.
    """
    existing: dict[str, Path] = {}
    if not IMAGE_DIR.exists():
        return existing

    known_files: set[str] = set()
    if record_index is not None:
        for file_hash, record in record_index.records_by_file_hash.items():
            file_name = str(record.get('file_name') or '').strip()
            if not file_name:
                continue
            candidate = IMAGE_DIR / Path(file_name).name
            if candidate.exists() and candidate.is_file():
                existing.setdefault(file_hash, candidate)
                known_files.add(candidate.name)

    for image_path in sorted(IMAGE_DIR.iterdir(), key=lambda path: path.name.lower()):
        if image_path.name in known_files:
            continue
        if not image_path.is_file() or image_path.name == 'rename_record.txt':
            continue
        if normalize_image_ext(image_path.suffix, fallback='') not in EXT_TO_PIL_FORMAT:
            continue
        try:
            existing.setdefault(_sha256_file(image_path), image_path)
        except OSError:
            continue
    return existing


# === 逐项下载工具的缓存索引(避免每张图重读整张 JSONL,把 O(N^2) 降到 O(N))===

# key: str(record_file) -> {'index', 'existing_image_hashes', 'record_mtime', 'image_dir'}
_GENERIC_DOWNLOAD_INDEX_CACHE: dict[str, dict] = {}


def _record_file_mtime(record_file: Path) -> float:
    try:
        return record_file.stat().st_mtime if record_file.exists() else 0.0
    except OSError:
        return 0.0


def _get_cached_download_index(
    record_filename: str = 'image_record.jsonl',
) -> tuple['DownloadRecordIndex', dict[str, Path], dict]:
    """
    返回 (内存索引, IMAGE_DIR 的 sha256->Path 索引, 缓存条目).

    跨多次 download_image_from_url 调用复用:只要 image_record.jsonl 的 mtime
    和当前 IMAGE_DIR 没变,就直接复用已建好的 O(1) 索引;变了才重建.
    本工具自己 append 写记录后会调用 _refresh_generic_index_mtime 刷新 mtime,
    避免把自己的写入误判成外部改动而反复重建.
    """
    record_file = _image_record_file(record_filename)
    key = str(record_file)
    record_mtime = _record_file_mtime(record_file)
    image_dir = str(IMAGE_DIR)
    cached = _GENERIC_DOWNLOAD_INDEX_CACHE.get(key)
    if (
        cached is not None
        and cached.get('record_mtime') == record_mtime
        and cached.get('image_dir') == image_dir
    ):
        return cached['index'], cached['existing_image_hashes'], cached

    index = _build_download_record_index(record_filename)
    existing_image_hashes = _build_existing_image_hash_index(index)
    cached = {
        'index': index,
        'existing_image_hashes': existing_image_hashes,
        'record_mtime': record_mtime,
        'image_dir': image_dir,
    }
    _GENERIC_DOWNLOAD_INDEX_CACHE[key] = cached
    return index, existing_image_hashes, cached


def _refresh_generic_index_mtime(cache_entry: dict) -> None:
    index = cache_entry.get('index')
    if index is None:
        return
    cache_entry['record_mtime'] = _record_file_mtime(index.record_file)
    cache_entry['image_dir'] = str(IMAGE_DIR)


def _safe_requested_image_sequence_from_index(
    requested_sequence: int,
    index: 'DownloadRecordIndex',
    file_prefix: str = 'temple',
) -> tuple[int, str]:
    """
    用内存索引的 max_sequence 取下一安全序号,免重读 JSONL.
    同时兼顾 image 目录里同前缀文件的最大序号,避免覆盖孤儿文件.
    """
    next_sequence = max(index.max_sequence, _max_image_file_sequence(file_prefix)) + 1
    if requested_sequence != next_sequence:
        return next_sequence, f'⚠️ agent 传入序号 {requested_sequence} 与当前下一安全序号不一致，已自动改为 {next_sequence}'
    return next_sequence, ''


# === 站点 hint 注册表:让逐项下载工具保持纯通用,站点差异通过注册下沉 ===

# 每项: {'hosts': tuple[str, ...], 'manifest': callable|None, 'invalid_collection': callable|None}
_SITE_DOWNLOAD_HINTS: list[dict] = []


def register_download_site_hint(
    hosts: list[str] | tuple[str, ...],
    *,
    manifest_from_page_url=None,
    is_invalid_collection_url=None,
    is_valid_page_url=None,
    item_id_from_urls=None,
    item_link_selector: str = '',
) -> None:
    """
    注册某站点的下载 hint:从详情页 URL 推导 IIIF manifest,非法 collection URL 判定,
    合法详情页 URL 判定,从 URL 提取站内 item id,以及搜索结果页中"item 详情链接"的 CSS 选择器
    (供 next_search_item 按 DOM 顺序枚举本页 item).
    download_image_from_url / next_search_item 通过通用分发调用这些 hint,新增站点只需在此注册,
    无需改下载/发号工具本身,通用核心也不含任何站点硬编码.
    """
    normalized_hosts = tuple(h.strip().lower() for h in hosts if h and h.strip())
    _SITE_DOWNLOAD_HINTS.append({
        'hosts': normalized_hosts,
        'manifest': manifest_from_page_url,
        'invalid_collection': is_invalid_collection_url,
        'valid_page': is_valid_page_url,
        'item_id': item_id_from_urls,
        'item_link_selector': (item_link_selector or '').strip(),
    })


def _matching_site_hints(url: str):
    host = (urlparse(_clean_url_text(url)).hostname or '').lower()
    if not host:
        return
    for hint in _SITE_DOWNLOAD_HINTS:
        if any(host == h or host.endswith('.' + h) for h in hint['hosts']):
            yield hint


def _site_manifest_url_from_page_url(page_url: str) -> str:
    """通用分发:若有站点 hint 能从详情页 URL 推导出 IIIF manifest,返回之;否则空串."""
    for hint in _matching_site_hints(page_url):
        fn = hint.get('manifest')
        if fn is None:
            continue
        try:
            result = fn(page_url)
        except Exception:
            continue
        if result:
            return result
    return ''


def _site_invalid_collection_url(url: str) -> bool:
    """通用分发:若有站点 hint 判定该 URL 为非法 collection/列表页,返回 True."""
    for hint in _matching_site_hints(url):
        fn = hint.get('invalid_collection')
        if fn is None:
            continue
        try:
            if fn(url):
                return True
        except Exception:
            continue
    return False


def _site_item_selector(url: str) -> str:
    """通用分发:返回该站点搜索结果页"item 详情链接"的 CSS 选择器;无则空串."""
    for hint in _matching_site_hints(url):
        selector = (hint.get('item_link_selector') or '').strip()
        if selector:
            return selector
    return ''


def _site_valid_page_url(url: str) -> bool:
    """通用分发:若有站点 hint 判定该 URL 为合法详情页,返回 True;无 hint 时返回 False."""
    for hint in _matching_site_hints(url):
        fn = hint.get('valid_page')
        if fn is None:
            continue
        try:
            if fn(url):
                return True
        except Exception:
            continue
    return False


def _site_item_id_from_urls(page_url: str, image_url: str = '') -> str:
    """通用分发:若有站点 hint 能从 URL 提取站内 item id,返回之;否则空串."""
    for raw_url in (page_url, image_url):
        for hint in _matching_site_hints(raw_url or ''):
            fn = hint.get('item_id')
            if fn is None:
                continue
            try:
                result = fn(page_url, image_url)
            except Exception:
                continue
            if result:
                return result
    return ''



def _append_image_record(record_file: Path, record: dict) -> None:
    record_file.parent.mkdir(parents=True, exist_ok=True)
    with open(record_file, 'a', encoding='utf-8') as file:
        file.write(json_module.dumps(record, ensure_ascii=False) + '\n')


def _append_image_info_record(data_dir: Path, record: dict, info_filename: str) -> Path:
    info_file = data_dir / _safe_agent_data_filename(info_filename, 'temple_photo_info.md')
    info_file.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not info_file.exists() or info_file.stat().st_size == 0
    with open(info_file, 'a', encoding='utf-8') as file:
        if needs_header:
            file.write(
                '# 图片下载记录\n\n'
                '| 序号 | 保存文件名 | 重命名标题 | 藏品标题 | 藏品 URL | 图片 URL | 相关证据 | 作者/时代/分类/馆藏号 | 简短说明 |\n'
                '|---|---|---|---|---|---|---|---|---|\n'
            )
        file.write(
            '| '
            + ' | '.join(
                [
                    _markdown_cell(record.get('sequence')),
                    _markdown_cell(record.get('file_name')),
                    _markdown_cell(record.get('title')),
                    _markdown_cell(record.get('collection_title')),
                    _markdown_cell(record.get('page_url')),
                    _markdown_cell(record.get('image_url')),
                    _markdown_cell(record.get('evidence')),
                    _markdown_cell(record.get('metadata')),
                    _markdown_cell(record.get('summary')),
                ]
            )
            + ' |\n'
        )
    return info_file


async def _record_saved_image_fast(
    *,
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
    record_index: DownloadRecordIndex,
    existing_image_hashes: dict[str, Path],
    precomputed_file_hash: str | None = None,
) -> ActionResult:
    """
    Fast batch-only record path: uses in-memory indexes and append-only writes.
    It intentionally avoids record_downloaded_image(), which reloads JSONL and
    rescans IMAGE_DIR on every call.

    ``precomputed_file_hash`` 允许调用方传入已经算好的 sha256,避免一张图被
    哈希两次(批量循环外面会先算一次用于内容去重).
    """
    try:
        data_dir = AGENT_DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)

        if not image_path.exists() or not image_path.is_file():
            return ActionResult(error=f'图片文件不存在，不能记录: {image_path}')
        try:
            _validate_saved_image_file(image_path, source='record_saved_image_fast')
        except RuntimeError as exc:
            return ActionResult(error=f'图片质量校验失败，拒绝记录: {exc}')

        if sequence in record_index.used_sequences:
            return ActionResult(error=f'序号 #{sequence} 已有记录，拒绝覆盖旧记录')

        file_hash = (precomputed_file_hash or '').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', file_hash or ''):
            file_hash = _sha256_file(image_path)
        existing_content_record = record_index.records_by_file_hash.get(file_hash)
        if existing_content_record:
            image_path.unlink(missing_ok=True)
            return ActionResult(
                extracted_content=(
                    f'✅ 图片内容已有下载记录，已删除本次重复文件并跳过\n'
                    f'- 已有序号: {existing_content_record.get("sequence")}\n'
                    f'- 已有文件: {existing_content_record.get("file_name", "")}\n'
                    f'- SHA256: {file_hash}'
                ),
                include_in_memory=True,
                long_term_memory=f'图片内容已记录，跳过重复记录: {existing_content_record.get("file_name", "")}',
            )

        existing_image_path = existing_image_hashes.get(file_hash)
        if existing_image_path and existing_image_path.resolve() != image_path.resolve():
            image_path.unlink(missing_ok=True)
            return ActionResult(
                extracted_content=(
                    f'✅ image 目录中已存在相同图片内容，已删除本次重复文件并跳过\n'
                    f'- 已有文件: {existing_image_path.name}\n'
                    f'- SHA256: {file_hash}'
                ),
                include_in_memory=True,
                long_term_memory=f'image 目录已有相同图片，跳过重复记录: {existing_image_path.name}',
            )

        normalized_title = _normalize_title(title, fallback=image_path.stem)
        source_hash = _source_hash(page_url, image_url, 0)
        if source_hash and source_hash in record_index.records_by_source_hash:
            image_path.unlink(missing_ok=True)
            existing_record = record_index.records_by_source_hash[source_hash]
            return ActionResult(
                extracted_content=(
                    f'✅ 来源已处理，已删除本次重复文件并跳过\n'
                    f'- 已有序号: {existing_record.get("sequence")}\n'
                    f'- source_hash: {source_hash}'
                ),
                include_in_memory=True,
                long_term_memory=f'来源已记录，跳过重复记录: {existing_record.get("file_name", "")}',
            )

        # 最终名后缀嵌入信息 hash(source_hash),让文件名自带"图片↔信息"绑定指纹;
        # 完整性仍用内容 sha256(file_hash) 校验(与文件名无关).
        embed_hash = source_hash or file_hash
        image_path = _rename_image_to_final_name(image_path, normalized_title, sequence, embed_hash)
        final_file_hash = _sha256_file(image_path)
        if final_file_hash != file_hash:
            raise RuntimeError(f'最终命名后图片 hash 变化: before={file_hash}, after={final_file_hash}')

        record = {
            'status': 'downloaded',
            'sequence': sequence,
            'file_name': image_path.name,
            'file_path': str(image_path),
            'file_size': image_path.stat().st_size,
            'sha256': file_hash,
            'content_hash': file_hash,
            'short_hash': file_hash[:8],
            'source_hash': source_hash,
            'source_item_id': _source_item_id_from_urls(page_url, image_url),
            'title_hash': _hash_text(normalized_title, 'sha1'),
            'title': normalized_title,
            'collection_title': _normalize_title(collection_title, fallback=title),
            'page_url': page_url.strip(),
            'image_url': image_url.strip(),
            'evidence': evidence.strip(),
            'metadata': metadata.strip(),
            'summary': summary.strip(),
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        }
        _append_image_record(record_index.record_file, record)
        record_index.add_record(record)
        existing_image_hashes[file_hash] = image_path
        info_file = _append_image_info_record(data_dir, record, info_filename)

        return ActionResult(
            extracted_content=(
                f'✅ 已快速记录图片 #{sequence}: {image_path.name}\n'
                f'- content_hash: {file_hash}\n'
                f'- source_hash: {source_hash}\n'
                f'- 当前有效记录: {record_index.downloaded_count}\n'
                f'- 信息表: {info_file}\n'
                f'- 结构化记录: {record_index.record_file}'
            ),
            include_in_memory=True,
            long_term_memory=f'已快速记录图片 #{sequence}: {image_path.name}，当前共 {record_index.downloaded_count} 条有效记录',
        )
    except Exception as e:
        return ActionResult(error=f'快速记录图片时出错: {str(e)}')


def _image_suffix_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return normalize_image_ext(suffix, fallback='.jpg')


def _image_suffix_from_content_type(content_type: str) -> str:
    normalized = content_type.lower().split(';', 1)[0].strip()
    mapping = {
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/webp': '.webp',
        'image/bmp': '.bmp',
        'image/tiff': '.tiff',
        'image/tif': '.tif',
    }
    return mapping.get(normalized, '.jpg')


def _content_type_is_json(content_type: str) -> bool:
    normalized = content_type.lower().split(';', 1)[0].strip()
    return normalized == 'application/json' or normalized.endswith('+json')


def normalize_image_ext(ext: str | None, fallback: str = '.png') -> str:
    if not ext:
        return fallback
    normalized = ext.lower()
    if not normalized.startswith('.'):
        normalized = f'.{normalized}'
    if normalized == '.jpe':
        return '.jpg'
    if normalized in EXT_TO_PIL_FORMAT:
        return normalized
    return fallback


def _pil_format_for_ext(ext: str) -> str:
    return EXT_TO_PIL_FORMAT.get(normalize_image_ext(ext), 'PNG')


def _safe_requested_image_filename(file_name: str, image_url: str) -> str:
    requested = Path(file_name or '').name.strip()
    suffix = _image_suffix_from_url(image_url)
    if not requested:
        requested = 'image'

    stem = Path(requested).stem if Path(requested).suffix else requested
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem)
    safe_stem = re.sub(r'\s+', '_', safe_stem).strip('._ ')[:180] or 'image'
    return f'{safe_stem}{suffix}'


def _safe_requested_image_filename_from_type(file_name: str, image_url: str, content_type: str) -> str:
    requested = Path(file_name or '').name.strip()
    suffix = Path(urlparse(image_url).path).suffix.lower()
    if suffix not in EXT_TO_PIL_FORMAT:
        suffix = _image_suffix_from_content_type(content_type)
    if not requested:
        requested = 'image'

    stem = Path(requested).stem if Path(requested).suffix else requested
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem)
    safe_stem = re.sub(r'\s+', '_', safe_stem).strip('._ ')[:180] or 'image'
    return f'{safe_stem}{normalize_image_ext(suffix, fallback=".jpg")}'


def _safe_png_filename(file_name: str) -> str:
    return _safe_image_filename_with_ext(file_name, '.png')


def _safe_image_filename_with_ext(file_name: str, preferred_ext: str) -> str:
    requested = Path(file_name or '').name.strip()
    stem = Path(requested).stem if requested else 'image'
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem)
    safe_stem = re.sub(r'\s+', '_', safe_stem).strip('._ ')[:180] or 'image'
    return f'{safe_stem}{normalize_image_ext(preferred_ext)}'


def _save_pil_image(image, output_path: Path, preferred_ext: str) -> Path:
    from PIL import ImageOps

    ext = normalize_image_ext(output_path.suffix or preferred_ext)
    output_path = _unique_path(output_path.with_suffix(ext))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    image_format = _pil_format_for_ext(ext)
    save_kwargs = {}
    prepared = ImageOps.exif_transpose(image)

    if image_format == 'PNG':
        save_kwargs['optimize'] = True
    elif image_format == 'JPEG':
        prepared = prepared.convert('RGB')
        save_kwargs.update({'quality': 95, 'subsampling': 0})
    elif image_format == 'TIFF':
        save_kwargs['compression'] = 'tiff_lzw'

    prepared.save(output_path, format=image_format, **save_kwargs)
    return output_path


def _validate_saved_image_file(image_path: Path, *, source: str) -> None:
    from PIL import Image, ImageStat

    min_size = 10 * 1024
    if not image_path.exists() or not image_path.is_file():
        raise RuntimeError(f'图片文件不存在: {image_path}')
    file_size = image_path.stat().st_size
    if file_size < min_size:
        raise RuntimeError(f'图片文件过小，疑似坏截图或占位图: {image_path.name} ({file_size} bytes, source={source})')

    try:
        with Image.open(image_path) as image:
            width, height = image.size
            if width < 200 or height < 200:
                raise RuntimeError(f'图片尺寸过小，疑似缩略图或坏截图: {image_path.name} ({width}x{height}, source={source})')

            sample = image.convert('RGB')
            sample.thumbnail((128, 128))
            extrema = ImageStat.Stat(sample).extrema
            if all((hi - lo) < 8 for lo, hi in extrema):
                raise RuntimeError(f'图片像素几乎单色，疑似空白/条纹坏截图: {image_path.name} (source={source})')
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f'无法验证图片文件有效性: {image_path.name} ({exc})') from exc


async def _download_image_to_file(
    url: str,
    target_file_name: str,
    timeout_seconds: int,
    referer: str | None,
    cookies: str | None,
) -> Path:
    target_dir = IMAGE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _unique_path(target_dir / _safe_requested_image_filename(target_file_name, url))
    tmp_path = target_path.with_suffix(target_path.suffix + '.part')

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    }
    if referer:
        headers['Referer'] = referer
    if cookies:
        headers['Cookie'] = cookies

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status >= 400:
                raise RuntimeError(f'HTTP {response.status}: {url}')
            content_type = response.headers.get('Content-Type', '')
            if 'image' not in content_type.lower():
                raise RuntimeError(f'URL 返回的不是图片内容: {content_type or "unknown"}')
            async with await anyio.open_file(tmp_path, 'wb') as f:
                async for chunk in response.content.iter_chunked(1024 * 256):
                    if chunk:
                        await f.write(chunk)

    if tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f'下载文件为空: {url}')

    tmp_path.replace(target_path)
    return target_path


def _write_image_bytes_to_file(
    image_bytes: bytes,
    target_file_name: str,
    image_url: str,
    content_type: str,
) -> Path:
    target_dir = IMAGE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _unique_path(target_dir / _safe_requested_image_filename_from_type(target_file_name, image_url, content_type))
    if not image_bytes:
        raise RuntimeError(f'图片字节为空: {image_url}')
    target_path.write_bytes(image_bytes)
    try:
        _validate_saved_image_file(target_path, source='browser_context_fetch')
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    return target_path


async def _browser_fetch_image_to_file(browser_session, image_url: str, target_file_name: str) -> Path:
    """
    在浏览器页面上下文中 fetch 图片,等价于使用当前浏览器会话/权限获取图片资源.
    这比驱动原生右键菜单稳定,也能使用站点 Cookie.
    """
    js_code = '''
    (async function(url) {
        try {
            const response = await fetch(url, {credentials: 'include', cache: 'no-store'});
            if (!response.ok) {
                return {success: false, error: `HTTP ${response.status}: ${response.statusText}`, url};
            }
            const blob = await response.blob();
            if (!blob.type || !blob.type.toLowerCase().startsWith('image/')) {
                return {success: false, error: `URL did not return image content: ${blob.type || 'unknown'}`, url};
            }
            const buffer = await blob.arrayBuffer();
            const bytes = new Uint8Array(buffer);
            const chunkSize = 0x8000;
            let binary = '';
            for (let i = 0; i < bytes.length; i += chunkSize) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
            }
            return {
                success: true,
                url: response.url || url,
                content_type: blob.type,
                byte_length: bytes.length,
                base64: btoa(binary),
            };
        } catch (error) {
            return {success: false, error: error.message, stack: error.stack, url};
        }
    })(''' + json_module.dumps(image_url, ensure_ascii=False) + ''')
    '''
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', '浏览器上下文保存图片失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', '浏览器上下文保存图片失败'))
    image_bytes = base64.b64decode(data.get('base64') or '')
    if data.get('byte_length') and len(image_bytes) != int(data['byte_length']):
        raise RuntimeError(f'图片字节长度不一致: expected={data["byte_length"]}, actual={len(image_bytes)}')
    return _write_image_bytes_to_file(
        image_bytes,
        target_file_name,
        data.get('url') or image_url,
        data.get('content_type') or '',
    )


def _trim_plain_border_from_image(image, black_threshold: int, white_threshold: int, border_ratio: float):
    """
    只裁掉几乎整行/整列都是黑色或白色的外边框,避免误裁作品本身的深色/浅色区域.
    """
    import numpy as np

    trimmed = image.convert('RGB')
    for _ in range(10):
        pixels = np.asarray(trimmed)
        if pixels.size == 0:
            return image

        black_mask = np.all(pixels <= black_threshold, axis=2)
        white_mask = np.all(pixels >= white_threshold, axis=2)
        height, width = black_mask.shape
        if height <= 2 or width <= 2:
            return trimmed

        def is_plain_row(index: int) -> bool:
            return black_mask[index, :].mean() >= border_ratio or white_mask[index, :].mean() >= border_ratio

        def is_plain_col(index: int) -> bool:
            return black_mask[:, index].mean() >= border_ratio or white_mask[:, index].mean() >= border_ratio

        top = 0
        while top < height - 1 and is_plain_row(top):
            top += 1

        bottom = height - 1
        while bottom > top and is_plain_row(bottom):
            bottom -= 1

        left = 0
        while left < width - 1 and is_plain_col(left):
            left += 1

        right = width - 1
        while right > left and is_plain_col(right):
            right -= 1

        if top == 0 and bottom == height - 1 and left == 0 and right == width - 1:
            return trimmed

        if right - left < max(10, width * 0.1) or bottom - top < max(10, height * 0.1):
            return trimmed

        trimmed = trimmed.crop((left, top, right + 1, bottom + 1))

    return trimmed


async def _navigate_to_image_url(browser_session, image_url: str) -> None:
    js_code = 'window.location.href = ' + json_module.dumps(image_url, ensure_ascii=False)
    cdp_session = await browser_session.get_or_create_cdp_session()
    await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': False},
        session_id=cdp_session.session_id,
    )
    await asyncio.sleep(2)


async def _get_visible_image_rect(browser_session) -> dict:
    js_code = '''
    (function() {
        const images = Array.from(document.images || []);
        const visible = images.map((img, index) => {
            const rect = img.getBoundingClientRect();
            const style = getComputedStyle(img);
            return {
                index,
                src: img.currentSrc || img.src || '',
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
                naturalWidth: img.naturalWidth || 0,
                naturalHeight: img.naturalHeight || 0,
                visible: rect.width > 2 && rect.height > 2 && style.visibility !== 'hidden' && style.display !== 'none',
            };
        }).filter(item => item.visible);

        visible.sort((a, b) => (b.width * b.height) - (a.width * a.height));
        const best = visible[0];
        if (!best) {
            return {success: false, error: '当前页面没有可见图片元素', page_url: window.location.href, page_title: document.title};
        }

        return {
            success: true,
            page_url: window.location.href,
            page_title: document.title,
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            device_pixel_ratio: window.devicePixelRatio || 1,
            image: best,
        };
    })()
    '''
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', '获取图片边界失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', '获取图片边界失败'))
    return data


async def _save_clean_visible_image_screenshot(
    browser_session,
    file_name: str,
    black_threshold: int,
    white_threshold: int,
    border_ratio: float,
    preferred_ext: str,
) -> tuple[Path, dict]:
    import io

    from PIL import Image

    rect_data = await _get_visible_image_rect(browser_session)
    screenshot_bytes = await browser_session.take_screenshot(full_page=False)
    screenshot = Image.open(io.BytesIO(screenshot_bytes))

    viewport_width = max(float(rect_data.get('viewport_width') or 1), 1.0)
    viewport_height = max(float(rect_data.get('viewport_height') or 1), 1.0)
    scale_x = screenshot.width / viewport_width
    scale_y = screenshot.height / viewport_height

    image_rect = rect_data['image']
    left = max(0, int(round(float(image_rect['x']) * scale_x)))
    top = max(0, int(round(float(image_rect['y']) * scale_y)))
    right = min(screenshot.width, int(round((float(image_rect['x']) + float(image_rect['width'])) * scale_x)))
    bottom = min(screenshot.height, int(round((float(image_rect['y']) + float(image_rect['height'])) * scale_y)))
    if right <= left or bottom <= top:
        raise RuntimeError(f'图片裁剪区域无效: {image_rect}')

    cropped = screenshot.crop((left, top, right, bottom))
    cleaned = _trim_plain_border_from_image(cropped, black_threshold, white_threshold, border_ratio)

    output_dir = IMAGE_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / _safe_image_filename_with_ext(file_name, preferred_ext)
    saved_path = _save_pil_image(cleaned, output_path, preferred_ext)
    try:
        _validate_saved_image_file(saved_path, source='clean_screenshot')
    except Exception:
        saved_path.unlink(missing_ok=True)
        raise
    return saved_path, rect_data


async def _detect_human_verification(browser_session) -> dict:
    """
    检测当前页面是否处于 Cloudflare / 人机验证页.
    """
    js_code = r'''
    (function() {
        const title = document.title || '';
        const body = (document.body && document.body.innerText) || '';
        const html = document.documentElement ? document.documentElement.innerHTML : '';
        const combined = `${title}\n${body}\n${html}`;
        const challengePattern = /cloudflare|verify you are human|checking if the site connection is secure|just a moment|cf-browser-verification|turnstile|请稍候|正在检查|人机验证|验证您是真人/i;
        const challengeIframe = document.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], iframe[title*="challenge" i], iframe[title*="Cloudflare" i]');
        const verifyControl = document.querySelector('input[type="checkbox"], button, iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]');
        const isChallenge = Boolean(challengeIframe || challengePattern.test(combined));
        return {
            url: window.location.href,
            title,
            is_challenge: isChallenge,
            has_verify_control: Boolean(verifyControl),
            text_sample: body.replace(/\s+/g, ' ').trim().slice(0, 240)
        };
    })()
    '''
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', '检测人机验证失败'))
    return result.get('result', {}).get('value') or {}


async def _collect_verification_click_targets(browser_session) -> dict:
    """
    收集页面上可能承载 Cloudflare/Turnstile 复选框的元素矩形(视口 CSS 像素坐标).

    Turnstile 复选框位于跨域 iframe(challenges.cloudflare.com)内,顶层文档无法用
    querySelector 穿透;因此这里只取容器 iframe / 小部件 / 顶层 checkbox 的矩形,
    具体点击点由调用方推算,再用 CDP Input.dispatchMouseEvent 在该坐标派发可信点击.
    """
    js_code = r'''
    (function() {
        const targets = [];
        const push = (el, kind) => {
            if (!el) return;
            const r = el.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) return;
            if (r.bottom < 0 || r.right < 0) return;
            targets.push({kind, x: r.x, y: r.y, width: r.width, height: r.height});
        };
        document.querySelectorAll(
            'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], '
            + 'iframe[title*="challenge" i], iframe[title*="Cloudflare" i], iframe[title*="verify" i]'
        ).forEach(f => push(f, 'cf_iframe'));
        document.querySelectorAll('.cf-turnstile, #challenge-stage, [class*="turnstile" i]').forEach(d => push(d, 'cf_widget'));
        document.querySelectorAll('input[type="checkbox"]').forEach(c => push(c, 'checkbox'));
        return {targets, vw: window.innerWidth, vh: window.innerHeight};
    })()
    '''
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', '定位验证控件失败'))
    return result.get('result', {}).get('value') or {}


def _verification_click_points(targets_info: dict) -> list[tuple[float, float]]:
    """
    把矩形列表转换成一组按优先级排序的候选点击坐标.

    - 顶层 checkbox:直接点中心.
    - 小尺寸 Turnstile 小部件/iframe(典型 ~300x65):复选框在左侧,点 (left+~30, 垂直中心).
    - 大尺寸全屏挑战 iframe:复选框通常在左上区域,按经验点 (left+45, top+55),并补一个中心点兜底.
    """
    points: list[tuple[float, float]] = []
    seen: set[tuple[int, int]] = set()

    def add(x: float, y: float) -> None:
        key = (round(x), round(y))
        if key in seen:
            return
        seen.add(key)
        points.append((x, y))

    targets = targets_info.get('targets') or []
    # checkbox 优先级最高
    for t in sorted(targets, key=lambda t: 0 if t.get('kind') == 'checkbox' else 1):
        x, y, w, h = t.get('x', 0), t.get('y', 0), t.get('width', 0), t.get('height', 0)
        kind = t.get('kind')
        if kind == 'checkbox':
            add(x + w / 2, y + h / 2)
        elif w <= 600 and h <= 160:
            add(x + min(34, w * 0.12), y + h / 2)
        else:
            add(x + 45, y + 55)
            add(x + w / 2, y + h / 2)
    return points


async def _cdp_click_point(browser_session, x: float, y: float) -> None:
    """用 CDP Input.dispatchMouseEvent 在视口坐标 (x, y) 派发一次可信左键点击.

    CDP 输入事件带 isTrusted=true,可穿透跨域 iframe 边界,满足 Turnstile 对用户手势的要求.
    """
    cdp_session = await browser_session.get_or_create_cdp_session()
    client = cdp_session.cdp_client
    sid = cdp_session.session_id
    await client.send.Input.dispatchMouseEvent(
        params={'type': 'mouseMoved', 'x': x, 'y': y}, session_id=sid
    )
    await asyncio.sleep(0.12)
    await client.send.Input.dispatchMouseEvent(
        params={'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'buttons': 1, 'clickCount': 1},
        session_id=sid,
    )
    await asyncio.sleep(0.07)
    await client.send.Input.dispatchMouseEvent(
        params={'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'buttons': 1, 'clickCount': 1},
        session_id=sid,
    )


async def _attempt_cloudflare_autoclick(browser_session, *, attempts: int, settle_seconds: float = 3.0) -> bool:
    """尝试自动点击 Cloudflare/Turnstile 复选框;挑战消失返回 True,否则 False.

    仅能处理"单击放行"型 managed challenge;交互式拼图类无法自动解,会回退人工.
    """
    for _ in range(attempts):
        state = await _detect_human_verification(browser_session)
        if not state.get('is_challenge'):
            return True
        try:
            targets_info = await _collect_verification_click_targets(browser_session)
        except RuntimeError:
            targets_info = {}
        points = _verification_click_points(targets_info)
        if not points:
            # 找不到可点控件(可能 iframe 尚未渲染),等待后重试.
            await asyncio.sleep(settle_seconds)
            continue
        for (x, y) in points:
            try:
                await _cdp_click_point(browser_session, x, y)
            except Exception:
                continue
            await asyncio.sleep(settle_seconds)
            after = await _detect_human_verification(browser_session)
            if not after.get('is_challenge'):
                return True
    return False


# === 注册自定义动作 ===


def _validate_public_image_url(image_url: str, allowed_host_suffixes: list[str] | None = None) -> str:
    url = _clean_url_text(image_url)
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {'http', 'https'}:
        raise RuntimeError(f'只允许下载 http(s) 图片 URL: {image_url}')

    host = (parsed.hostname or '').strip().lower().rstrip('.')
    if not host:
        raise RuntimeError(f'图片 URL 缺少有效域名: {image_url}')

    if host in {'localhost', 'localhost.localdomain'} or host.endswith(('.localhost', '.local', '.internal')):
        raise RuntimeError(f'拒绝下载本地或内部域名图片: {image_url}')

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        raise RuntimeError(f'拒绝下载本地或内网 IP 图片: {image_url}')

    allowed = _sanitize_allowed_host_suffixes(allowed_host_suffixes)
    if allowed and not any(host == suffix or host.endswith(f'.{suffix}') for suffix in allowed):
        raise RuntimeError(f'图片域名 {host} 不在允许列表中: {", ".join(allowed)}')

    return url


def _looks_like_iiif_manifest_url(url: str) -> bool:
    parsed = urlparse((url or '').strip())
    path = parsed.path.lower().rstrip('/')
    return '/iiif/' in path and ('/manifest/' in path or path.endswith('/manifest'))


def _looks_like_iiif_image_service_url(url: str) -> bool:
    parsed = urlparse((url or '').strip())
    path = parsed.path.strip('/')
    parts = path.split('/')
    if len(parts) < 4:
        return False
    return parts[0] == 'image' and parts[1] == 'iiif' and parts[2] in {'2', '3'}


def _iiif_service_to_default_image_url(service_url: str) -> str:
    clean_url = (service_url or '').strip().rstrip('/')
    if not clean_url:
        return clean_url
    if re.search(r'/full/[^/]+/[^/]+/default\.(?:jpe?g|png|webp|tiff?)$', urlparse(clean_url).path, re.IGNORECASE):
        return clean_url
    if _looks_like_iiif_image_service_url(clean_url):
        return f'{clean_url}/full/max/0/default.jpg'
    return clean_url


def _iiif_manifest_image_score(url: str) -> int:
    score = 0
    if '/image/iiif/' in url:
        score += 500
    if '/full/' in url:
        score += 250
    if re.search(r'/full/(?:max|full)/', url, re.IGNORECASE):
        score += 120
    if re.search(r'/default\.(?:jpe?g|png|webp|tiff?)$', urlparse(url).path, re.IGNORECASE):
        score += 80
    if re.search(r'preview|thumb|thumbnail|small', url, re.IGNORECASE):
        score -= 300
    return score


def _collect_iiif_manifest_image_urls(value: object) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add_url(raw_url: object) -> None:
        if not isinstance(raw_url, str) or not raw_url.strip():
            return
        url = raw_url.strip()
        if _looks_like_iiif_image_service_url(url):
            url = _iiif_service_to_default_image_url(url)
        if url.startswith(('http://', 'https://')) and url not in seen:
            seen.add(url)
            urls.append(url)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            node_type = str(node.get('type') or '').lower()
            node_id = node.get('id') or node.get('@id')
            if node_type == 'image':
                add_url(node_id)
            if isinstance(node_id, str) and _looks_like_iiif_image_service_url(node_id):
                add_url(node_id)
            service = node.get('service') or node.get('services')
            if isinstance(service, dict):
                add_url(service.get('id') or service.get('@id'))
            elif isinstance(service, list):
                for item in service:
                    if isinstance(item, dict):
                        add_url(item.get('id') or item.get('@id'))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(urls, key=lambda item: (_iiif_manifest_image_score(item), item), reverse=True)


async def _fetch_json_url(
    url: str,
    timeout_seconds: int,
    referer: str | None = None,
    cookies: str | None = None,
) -> tuple[object, str]:
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept': 'application/json,application/ld+json;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if referer:
        headers['Referer'] = referer
    if cookies:
        headers['Cookie'] = cookies

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status >= 400:
                raise RuntimeError(f'HTTP {response.status}: {url}')
            content_type = response.headers.get('Content-Type', '')
            text = await response.text()
            if not _content_type_is_json(content_type):
                try:
                    return json_module.loads(text), response.url.human_repr()
                except json_module.JSONDecodeError as exc:
                    raise RuntimeError(f'URL 返回的不是 JSON manifest: {content_type or "unknown"}') from exc
            return json_module.loads(text), response.url.human_repr()


async def _resolve_iiif_manifest_to_image_url(
    manifest_url: str,
    allowed_host_suffixes: list[str] | None,
    timeout_seconds: int,
    referer: str | None,
    cookies: str | None,
) -> tuple[str, int]:
    manifest_data, final_manifest_url = await _fetch_json_url(manifest_url, timeout_seconds, referer, cookies)
    candidates = _collect_iiif_manifest_image_urls(manifest_data)
    for candidate in candidates:
        try:
            return _validate_public_image_url(candidate, allowed_host_suffixes), len(candidates)
        except RuntimeError:
            continue
    raise RuntimeError(f'IIIF manifest 未找到可下载图片 URL: {final_manifest_url}')


async def _extract_generic_image_candidates(browser_session, allowed_host_suffixes: list[str] | None = None) -> dict:
    js_code = '''
    (function() {
        try {
            const candidates = [];
            const seen = new Set();
            const addUrl = (raw, source, element) => {
                if (!raw) return;
                String(raw).split(',').forEach(part => {
                    const value = part.trim().split(/\\s+/)[0];
                    if (!value || value.startsWith('data:') || value.startsWith('blob:') || value.startsWith('javascript:')) return;
                    try {
                        const url = new URL(value, window.location.href).href;
                        if (!/^https?:\\/\\//i.test(url) || seen.has(url)) return;
                        seen.add(url);
                        let area = 0;
                        let naturalArea = 0;
                        let alt = '';
                        if (element && element.getBoundingClientRect) {
                            const rect = element.getBoundingClientRect();
                            area = Math.max(0, rect.width) * Math.max(0, rect.height);
                            alt = element.getAttribute('alt') || element.getAttribute('aria-label') || '';
                        }
                        if (element && element.tagName && element.tagName.toLowerCase() === 'img') {
                            naturalArea = (element.naturalWidth || 0) * (element.naturalHeight || 0);
                        }
                        candidates.push({url, source, area, naturalArea, alt});
                    } catch (_) {}
                });
            };

            document.querySelectorAll('img, source, a, video, object, embed').forEach((element) => {
                addUrl(element.currentSrc, 'currentSrc', element);
                addUrl(element.src, 'src', element);
                addUrl(element.srcset, 'srcset', element);
                addUrl(element.href, 'href', element);
                addUrl(element.data, 'data', element);
                addUrl(element.getAttribute('src'), 'attr-src', element);
                addUrl(element.getAttribute('srcset'), 'attr-srcset', element);
                addUrl(element.getAttribute('href'), 'attr-href', element);
                addUrl(element.getAttribute('data-src'), 'data-src', element);
                addUrl(element.getAttribute('data-original'), 'data-original', element);
                addUrl(element.getAttribute('data-full'), 'data-full', element);
                addUrl(element.getAttribute('data-image'), 'data-image', element);
                addUrl(element.getAttribute('data-iiif'), 'data-iiif', element);
            });

            document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"], link[rel*="image" i]').forEach((element) => {
                addUrl(element.content, 'meta', element);
                addUrl(element.href, 'link', element);
            });

            document.querySelectorAll('*').forEach((element) => {
                const bg = getComputedStyle(element).backgroundImage || '';
                for (const match of bg.matchAll(/url\\(["']?([^"')]+)["']?\\)/g)) {
                    addUrl(match[1], 'background', element);
                }
            });

            const html = document.documentElement.outerHTML;
            for (const match of html.matchAll(/https?:\\/\\/[^"'<>\\s]+\\/(?:image\\/iiif|iiif|art_images|images?|media|assets)\\/[^"'<>\\s]+/gi)) {
                addUrl(match[0], 'html');
            }

            const score = (item) => {
                const url = item.url || '';
                let value = 0;
                if (/\\/iiif\\//i.test(url) || /image\\/iiif/i.test(url)) value += 300;
                if (/\\/full\\/(?:max|full|\\^?!?\\d)/i.test(url)) value += 180;
                if (/(?:large|original|full|master|default)\\.(?:jpe?g|png|webp|tiff?)(?:[?#]|$)/i.test(url)) value += 120;
                if (/\\.(?:jpe?g|png|webp|tiff?)(?:[?#]|$)/i.test(url)) value += 80;
                if (/-L\\.(?:jpe?g|png|webp)(?:[?#]|$)/i.test(url)) value += 80;
                if (/-M\\.(?:jpe?g|png|webp)(?:[?#]|$)/i.test(url)) value += 40;
                if (/thumb|thumbnail|small|icon|logo|sprite|avatar|button|banner/i.test(url)) value -= 180;
                if (/download/i.test(item.source || '')) value += 20;
                value += Math.min(item.naturalArea || 0, 4000000) / 40000;
                value += Math.min(item.area || 0, 1000000) / 20000;
                return value;
            };

            const ordered = candidates
                .filter(item => !/\\.svg(?:[?#]|$)/i.test(item.url || ''))
                .sort((a, b) => score(b) - score(a) || a.url.localeCompare(b.url));

            return {
                success: true,
                page_url: window.location.href,
                page_title: document.title,
                image_urls: ordered.map(item => item.url),
                candidates: ordered.slice(0, 80),
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
        raise RuntimeError(result['exceptionDetails'].get('text', '通用图片候选提取失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', '通用图片候选提取失败'))

    valid_urls: list[str] = []
    valid_candidates: list[dict] = []
    for candidate in data.get('candidates') or []:
        raw_url = str(candidate.get('url') or '')
        try:
            valid_url = _validate_public_image_url(raw_url, allowed_host_suffixes)
        except RuntimeError:
            continue
        valid_urls.append(valid_url)
        valid_candidates.append({**candidate, 'url': valid_url})

    data['image_urls'] = valid_urls
    data['candidates'] = valid_candidates
    return data


def _resolve_generic_image_url(
    params_image_url: str,
    page_url: str,
    page_data: dict,
    image_index: int,
    allowed_host_suffixes: list[str] | None,
) -> tuple[str, list[str]]:
    candidates = [str(url) for url in (page_data.get('image_urls') or []) if url]
    if params_image_url.strip():
        return _validate_public_image_url(urljoin(page_url or page_data.get('page_url', ''), params_image_url.strip()), allowed_host_suffixes), candidates
    if not candidates:
        raise RuntimeError('当前页面未找到可下载的公网图片 URL;如页面已显示大图,可允许 clean_screenshot 兜底.')
    if image_index >= len(candidates):
        raise RuntimeError(f'图片索引 {image_index} 超出范围，当前找到 {len(candidates)} 个通用图片候选。')
    return _validate_public_image_url(candidates[image_index], allowed_host_suffixes), candidates


def _read_downloaded_records(record_file: Path | str) -> list[dict]:
    if not isinstance(record_file, Path):
        record_file = _image_record_file(str(record_file))
    if not record_file.exists():
        return []
    records: list[dict] = []
    for line in record_file.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json_module.loads(line)
        except json_module.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get('status') == 'downloaded':
            records.append(record)
    return records


def _image_hash_groups() -> list[dict]:
    groups: dict[str, list[str]] = {}
    if not IMAGE_DIR.exists():
        return []
    for image_path in sorted(IMAGE_DIR.iterdir(), key=lambda path: path.name.lower()):
        if not image_path.is_file() or image_path.name == 'rename_record.txt':
            continue
        if normalize_image_ext(image_path.suffix, fallback='') not in EXT_TO_PIL_FORMAT:
            continue
        try:
            file_hash = _sha256_file(image_path)
        except OSError:
            continue
        groups.setdefault(file_hash, []).append(image_path.name)
    return [
        {'sha256': file_hash, 'files': files}
        for file_hash, files in groups.items()
        if len(files) > 1
    ]


def validate_download_artifacts(
    target_count: int = 100,
    record_filename: str = 'image_record.jsonl',
    *,
    validate_image_files: bool = True,
    include_duplicate_hash_groups: bool = True,
) -> dict:
    data_dir = AGENT_DATA_DIR
    record_file = data_dir / _safe_agent_data_filename(record_filename, 'image_record.jsonl')
    records = _read_downloaded_records(record_file)
    sequences = sorted({int(record.get('sequence') or 0) for record in records if str(record.get('sequence') or '').isdigit()})
    missing_sequences = [sequence for sequence in range(1, target_count + 1) if sequence not in sequences]
    image_files = [
        path for path in IMAGE_DIR.iterdir()
        if IMAGE_DIR.exists()
        and path.is_file()
        and path.name != 'rename_record.txt'
        and normalize_image_ext(path.suffix, fallback='') in EXT_TO_PIL_FORMAT
    ] if IMAGE_DIR.exists() else []

    bad_files: list[dict] = []
    record_file_names = set()
    for record in records:
        for key in ('file_name', 'final_file_name'):
            value = Path(str(record.get(key) or '')).name
            if value:
                record_file_names.add(value)
        title = str(record.get('title') or '').strip()
        if title:
            for ext in EXT_TO_PIL_FORMAT:
                record_file_names.add(f'{title}{ext}')
    if validate_image_files:
        for image_path in image_files:
            try:
                _validate_saved_image_file(image_path, source='final_validation')
            except RuntimeError as exc:
                bad_files.append({'file': image_path.name, 'error': str(exc)})

    orphan_files = sorted(path.name for path in image_files if path.name not in record_file_names)
    duplicate_hash_groups = _image_hash_groups() if include_duplicate_hash_groups else []
    complete = (
        len(records) >= target_count
        and len(image_files) >= target_count
        and (not validate_image_files or not bad_files)
        and (not include_duplicate_hash_groups or not duplicate_hash_groups)
    )
    remaining_records = max(0, target_count - len(records))
    return {
        'complete': complete,
        'target_count': target_count,
        'downloaded_records': len(records),
        'image_file_count': len(image_files),
        'remaining_records': remaining_records,
        'missing_sequences': missing_sequences,
        'bad_files': bad_files,
        'orphan_files': orphan_files,
        'duplicate_hash_groups': duplicate_hash_groups,
        'validate_image_files': validate_image_files,
        'include_duplicate_hash_groups': include_duplicate_hash_groups,
        'record_file': str(record_file),
    }


def format_download_validation_report(validation: dict) -> str:
    status = 'SUCCESS' if validation.get('complete') else 'INCOMPLETE'
    lines = [
        f'Final download validation: {status}',
        f"- target_count: {validation.get('target_count')}",
        f"- downloaded_records: {validation.get('downloaded_records')}",
        f"- remaining_records_needed: {validation.get('remaining_records')}",
        f"- image_files: {validation.get('image_file_count')}",
        f"- sequence_gaps_warning_only: {validation.get('missing_sequences') or 'none'}",
        f"- bad_or_empty_images: {len(validation.get('bad_files') or [])}",
        f"- duplicate_image_hash_groups: {len(validation.get('duplicate_hash_groups') or [])}",
        f"- image_file_validation: {'enabled' if validation.get('validate_image_files') else 'skipped_for_batch'}",
        f"- duplicate_hash_scan: {'enabled' if validation.get('include_duplicate_hash_groups') else 'skipped_for_batch'}",
        f"- orphan_files_warning_only: {len(validation.get('orphan_files') or [])}",
        f"- record_file: {validation.get('record_file')}",
    ]
    if validation.get('bad_files'):
        lines.append('- bad_image_details:')
        lines.extend(f"  - {item['file']}: {item['error']}" for item in validation['bad_files'][:20])
    if validation.get('orphan_files'):
        lines.append('- orphan_files_first_20: ' + ', '.join(validation['orphan_files'][:20]))
    return '\n'.join(lines)


async def _current_browser_url(browser_session) -> str:
    js_code = 'window.location.href'
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': False},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        return ''
    return str(result.get('result', {}).get('value') or '')


# ---------------------------------------------------------------------------
# 搜索结果页 item 游标(next_search_item 工具用)
#
# 解决"逐 item 通用下载流程没有页内序号游标"的缺口:进入搜索结果页后按 DOM 顺序
# (左→右,上→下)枚举本页所有 item,以 image_record.jsonl(真实下载记录)+ 本游标
# 文件为"已处理"事实来源,统一发号"下一个该处理的 item 序号 + URL",避免 LLM 忘记
# 处理到哪个而导致的错位 / 跳过 / 重复循环.
# ---------------------------------------------------------------------------

SEARCH_ITEM_CURSOR_FILE = 'search_item_cursor.json'


def _search_item_cursor_file() -> Path:
    return AGENT_DATA_DIR / SEARCH_ITEM_CURSOR_FILE


def _load_search_item_cursor() -> dict:
    cursor_file = _search_item_cursor_file()
    if not cursor_file.exists():
        return {}
    try:
        data = json_module.loads(cursor_file.read_text(encoding='utf-8'))
    except json_module.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_search_item_cursor(data: dict) -> Path:
    cursor_file = _search_item_cursor_file()
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    cursor_file.write_text(
        json_module.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return cursor_file


_SEARCH_ITEM_ENUM_JS_TEMPLATE = r'''
(function() {
    try {
        const selector = __SELECTOR__;
        const seen = new Set();
        const items = [];
        const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const nodes = document.querySelectorAll(selector);
        for (const link of nodes) {
            const href = link.href || link.getAttribute('href') || '';
            let url;
            try {
                url = new URL(href, window.location.href).href;
            } catch (_) {
                continue;
            }
            // 归一化:去 fragment,去末尾斜杠,与 Python 端 _normalize_source_url 对齐
            let key = url.split('#')[0];
            if (key.length > 1 && key.endsWith('/')) key = key.slice(0, -1);
            key = key.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            const container = link.closest('article, li, .card, .result, .item, .collection-item') || link;
            const title = clean(
                link.getAttribute('title') ||
                link.textContent ||
                (container && container.getAttribute && container.getAttribute('aria-label')) ||
                (container && container.textContent) ||
                ''
            );
            items.push({ url: url, title: title.slice(0, 300) });
        }
        return {
            success: true,
            page_url: window.location.href,
            page_title: document.title,
            total_found: items.length,
            items: items.slice(0, __CAP__),
        };
    } catch (error) {
        return { success: false, error: String(error && error.message || error) };
    }
})()
'''


def _build_search_item_enum_js(selector: str, cap: int) -> str:
    return (
        _SEARCH_ITEM_ENUM_JS_TEMPLATE
        .replace('__SELECTOR__', json_module.dumps(selector))
        .replace('__CAP__', json_module.dumps(int(cap)))
    )


async def _enumerate_current_page_items(browser_session, selector: str, cap: int = 500) -> dict:
    """在当前 tab 按 DOM 顺序(左→右,上→下)枚举 item 详情链接,去重后返回有序清单."""
    js_code = _build_search_item_enum_js(selector, cap)
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        exc = result['exceptionDetails']
        detail = (exc.get('exception') or {}).get('description') or exc.get('text') or '页面 item 枚举失败'
        return {'success': False, 'error': str(detail), 'items': []}
    data = result.get('result', {}).get('value') or {}
    if not isinstance(data, dict):
        return {'success': False, 'error': '页面 item 枚举返回异常', 'items': []}
    return data


def _recorded_page_urls(record_filename: str = 'image_record.jsonl') -> set[str]:
    """从 image_record.jsonl 收集已成功下载过的 item 详情页 URL(归一化),作为已处理事实来源."""
    processed: set[str] = set()
    for record in _load_image_records(_image_record_file(record_filename)):
        page_url = record.get('page_url') or ''
        if page_url:
            processed.add(_normalize_source_url(page_url))
    return processed


def _select_next_search_item(
    *,
    items: list[dict],
    current_page_url: str,
    keyword: str,
    mark_done_url: str = '',
    record_filename: str = 'image_record.jsonl',
) -> dict:
    """
    统一发号核心逻辑:
    - 以"游标 done 集合 ∪ image_record.jsonl 中已记录的 page_url"为已处理事实来源;
    - 返回 DOM 顺序里第一个未处理的 item(序号 + URL + title);全处理完则标记本页 done.
    游标按归一化后的搜索结果页 URL 作 key,跨调用 / 跨进程续跑稳定.
    """
    page_key = _normalize_source_url(current_page_url)
    cursor = _load_search_item_cursor()
    cursor.setdefault('pages', {})
    page_state = cursor['pages'].setdefault(page_key, {
        'page_url': current_page_url,
        'done': [],
        'handed': [],
    })
    page_state['page_url'] = current_page_url

    done_set = {_normalize_source_url(u) for u in page_state.get('done') or []}
    if mark_done_url:
        norm_done = _normalize_source_url(mark_done_url)
        if norm_done and norm_done not in done_set:
            done_set.add(norm_done)
            page_state.setdefault('done', []).append(mark_done_url)

    processed = done_set | _recorded_page_urls(record_filename)

    ordered = [it for it in items if isinstance(it, dict) and it.get('url')]
    next_item = None
    next_index = -1
    for idx, it in enumerate(ordered):
        if _normalize_source_url(it['url']) not in processed:
            next_item = it
            next_index = idx
            break

    processed_count = sum(1 for it in ordered if _normalize_source_url(it['url']) in processed)
    page_state['total_found'] = len(ordered)
    page_state['processed_count'] = processed_count
    page_state['next_index'] = next_index if next_index >= 0 else len(ordered)
    page_state['updated_at'] = datetime.now(timezone.utc).isoformat()
    if next_item is not None:
        handed = page_state.setdefault('handed', [])
        if next_item['url'] not in handed:
            handed.append(next_item['url'])
    else:
        page_state['status'] = 'done'

    cursor['keyword'] = keyword
    cursor['active_page_url'] = current_page_url
    cursor['updated_at'] = page_state['updated_at']
    _write_search_item_cursor(cursor)

    return {
        'next_item': next_item,
        'next_index': next_index,
        'total_found': len(ordered),
        'processed_count': processed_count,
        'page_key': page_key,
    }


def _generic_image_strategy_file() -> Path:
    return AGENT_DATA_DIR / 'generic_image_download_strategy.json'


def _load_generic_image_strategy() -> dict:
    strategy_file = _generic_image_strategy_file()
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


def _write_generic_image_strategy(strategy: dict) -> Path:
    strategy_file = _generic_image_strategy_file()
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text(json_module.dumps(strategy, ensure_ascii=False, indent=2), encoding='utf-8')
    return strategy_file


def _ordered_generic_image_methods(strategy: dict, prefer_browser_fetch: bool, allow_clean_screenshot: bool) -> list[str]:
    base_methods = ['browser_context_fetch', 'python_direct'] if prefer_browser_fetch else ['python_direct', 'browser_context_fetch']
    if allow_clean_screenshot:
        base_methods.append('clean_screenshot')
    preferred = str(strategy.get('preferred_method') or '')
    if preferred in base_methods:
        return [preferred, *[method for method in base_methods if method != preferred]]
    return base_methods


def _record_generic_image_method_success(method: str, sequence: int, image_url: str) -> dict:
    strategy = _load_generic_image_strategy()
    if method == strategy.get('last_method'):
        streak = int(strategy.get('streak') or 0) + 1
    else:
        streak = 1

    strategy['last_method'] = method
    strategy['streak'] = streak
    if streak >= GENERAL_IMAGE_STRATEGY_LOCK_THRESHOLD:
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
    _write_generic_image_strategy(strategy)
    return strategy


def _record_generic_image_method_failure(method: str, sequence: int, image_url: str, error: str) -> dict:
    strategy = _load_generic_image_strategy()
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
    _write_generic_image_strategy(strategy)
    return strategy


async def _save_generic_image_by_method(
    method: str,
    browser_session,
    image_url: str,
    file_name: str,
    page_url: str,
    referer: str,
    timeout_seconds: int,
    use_browser_cookies: bool,
    black_threshold: int,
    white_threshold: int,
    border_ratio: float,
) -> Path:
    if method == 'python_direct':
        cookie_header = ''
        if use_browser_cookies:
            cookie_header = await _get_browser_cookie_header(browser_session, [image_url, page_url])
        return await _download_image_to_file(
            image_url,
            file_name,
            timeout_seconds,
            referer=referer or page_url or None,
            cookies=cookie_header or None,
        )

    if method == 'browser_context_fetch':
        return await _browser_fetch_image_to_file(browser_session, image_url, file_name)

    if method == 'clean_screenshot':
        if image_url:
            await _navigate_to_image_url(browser_session, image_url)
        image_path, _ = await _save_clean_visible_image_screenshot(
            browser_session,
            file_name,
            black_threshold=black_threshold,
            white_threshold=white_threshold,
            border_ratio=border_ratio,
            preferred_ext=_image_suffix_from_url(image_url) if image_url else '.png',
        )
        return image_path

    raise RuntimeError(f'未知通用图片保存方法: {method}')


def _existing_recorded_image_urls(record_filename: str) -> set[str]:
    record_file = AGENT_DATA_DIR / _safe_agent_data_filename(record_filename, 'image_record.jsonl')
    return {
        str(record.get('image_url') or '').strip()
        for record in _load_image_records(record_file)
        if record.get('status') == 'downloaded' and record.get('image_url')
    }


def _format_output(
    found_blocks: list[dict],
    page_title: str,
    page_url: str,
    format_type: str,
) -> tuple[str, str]:
    """根据格式类型生成输出内容,返回 (content, file_extension)."""
    fmt = format_type.lower()

    if fmt == 'json':
        content = json_module.dumps(
            {
                'page_title': page_title,
                'url': page_url,
                'total_found_blocks': len(found_blocks),
                'found_blocks': found_blocks,
            },
            ensure_ascii=False,
            indent=2,
        )
        return content, '.json'

    if fmt == 'text':
        lines = [
            f"页面标题: {page_title}",
            f"URL: {page_url}",
            f"找到 {len(found_blocks)} 个匹配的HTML代码块",
            "=" * 80,
            "",
        ]
        for i, block in enumerate(found_blocks, 1):
            lines.append(f"--- 匹配块 {i} ---")
            lines.append(f"原始起始行: {block.get('original_start', '')}")
            lines.append(f"原始结束行: {block.get('original_end', '')}")
            lines.append("提取的HTML代码:")
            lines.append(block.get('content', ''))
            lines.append("")
        return "\n".join(lines), '.txt'

    # markdown(默认)
    md = f"# {page_title}\n\n"
    md += f"**URL**: {page_url}\n\n"
    md += f"**找到匹配的HTML代码块数量**: {len(found_blocks)}\n\n"
    md += "---\n\n"
    for i, block in enumerate(found_blocks, 1):
        md += f"## 匹配块 {i}\n\n"
        md += f"**原始起始行**: `{block.get('original_start', '')}`\n\n"
        md += f"**原始结束行**: `{block.get('original_end', '')}`\n\n"
        md += f"**提取的HTML代码**:\n```html\n{block.get('content', '')}\n```\n\n"
    return md, '.md'


# === 下载格式选择工具 ===


# === 工具拆分:导入 tool_actions/ 下各工具,触发 @tools.action 注册并 re-export ===
# 必须放在文件末尾--此处所有共享 helper / 参数模型 / 运行时全局均已定义,
# 各 tool 模块 `from tools_registry import ...` 才能解析成功(标准的“底部注册”模式).
#
# 先导入可插拔站点模块(sites/),触发其 register_download_site_hint 注入,并把
# 站点特有的参数模型 / 进度 helper re-export 到本模块命名空间,供下游
# tool_actions / main.py 继续 `from tools_registry import ...`(保持调用点不变).
from sites.idp import (  # noqa: E402,F401
    NavigateIdpSearchPageParams,
    DownloadCurrentIdpSearchPageImagesParams,
    _idp_progress_file,
    _load_idp_progress,
    _write_idp_progress,
)

from tool_actions.wait_for_human_verification import wait_for_human_verification  # noqa: E402,F401
from tool_actions.record_downloaded_image import record_downloaded_image  # noqa: E402,F401
from tool_actions.validate_download_completion import validate_download_completion  # noqa: E402,F401
from tool_actions.finish_download_task import finish_download_task  # noqa: E402,F401
from tool_actions.navigate_idp_search_page import navigate_idp_search_page  # noqa: E402,F401
from tool_actions.download_current_idp_search_page_images import download_current_idp_search_page_images  # noqa: E402,F401

# === LOC / Kyohaku 站点特有工具(已物理迁出到 legacy/site_tools.py)===
# 仅 import 触发其内部 @legacy_tools_action 注册(默认不注册,需
# BROWSER_USE_ENABLE_LEGACY_TOOLS=1 才进入 agent 工具目录).必须放在所有
# tool_actions 导入之后--legacy 模块会 `from tools_registry import record_downloaded_image`
# 等底部才定义的名字.
import legacy.site_tools  # noqa: E402,F401
