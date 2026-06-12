"""
工具注册模块 - 自定义 browser-use Agent 工具

将工具定义从 main.py 中提取出来，便于管理和复用。
包含：
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
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp
import anyio
from pydantic import BaseModel, Field

from browser_use import ActionResult, Tools
from idp_page_progress import load_page_progress, mark_page_batch_result

# 使用脚本所在目录作为项目基准路径；运行产物目录可由 main.py 动态配置
PROJECT_DIR = Path(__file__).resolve().parent
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
KYOHAKU_METHODS = ('python_direct', 'browser_context_fetch', 'clean_screenshot')
KYOHAKU_STRATEGY_LOCK_THRESHOLD = 5
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
    information_file_path: str = str(Path(__file__).resolve().parent / "Information.md")


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


class WaitForHumanVerificationParams(BaseModel):
    """等待人工完成人机验证的参数模型"""
    timeout_seconds: int = Field(default=180, ge=1, le=900, description='最多等待人工完成验证的秒数')
    poll_interval_seconds: int = Field(default=5, ge=1, le=30, description='检查页面是否恢复的间隔秒数')


class RebuildLocDownloadStateParams(BaseModel):
    """重建 LOC 下载状态的参数模型"""
    remove_irrelevant: bool = Field(default=True, description='是否从队列中移除明显不相关的 LOC 条目')
    reset_in_progress: bool = Field(default=True, description='是否把运行中断留下的 in_progress 重置为 pending')
    rewrite_title_file: bool = Field(default=True, description='是否根据成功下载记录重写 title.txt，并自动备份旧文件')


class RecordDownloadedImageParams(BaseModel):
    """记录已保存图片的参数模型"""
    sequence: int = Field(ge=1, description='图片序号，从 1 开始，应与保存文件名顺序一致')
    file_name: str = Field(description='已保存到 image 目录中的文件名，例如 temple_001.png')
    title: str = Field(description='用于最终重命名的短标题，例如 寺_001_藏品标题_图1')
    collection_title: str = Field(default='', description='藏品页面显示的原始标题')
    page_url: str = Field(default='', description='藏品详情页 URL')
    image_url: str = Field(default='', description='原始图片 URL，优先使用 /art_images/...-L.jpg')
    evidence: str = Field(default='', description='判断与关键词相关的证据')
    metadata: str = Field(default='', description='作者、时代、分类、馆藏号等信息')
    summary: str = Field(default='', description='图片或藏品的简短中文说明')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')


class DownloadImageFromUrlParams(BaseModel):
    """通用图片 URL 下载并记录的参数模型"""
    sequence: int = Field(ge=1, description='图片序号，从 1 开始；工具会自动修正为当前下一安全序号，避免覆盖')
    file_name: str = Field(default='temple_001', description='保存文件名或基础名，例如 temple_001；扩展名会优先使用图片 URL 或响应类型')
    title: str = Field(description='用于最终重命名的短标题，例如 china_temple_001_藏品标题_图1')
    collection_title: str = Field(default='', description='藏品页面显示的原始标题')
    page_url: str = Field(default='', description='藏品详情页 URL；也会作为默认 Referer')
    image_url: str = Field(default='', description='可直接访问的图片 URL，例如 IIIF /full/max/0/default.jpg、IIIF manifest URL 或 viewer 大图 URL；为空时会从当前页面自动查找候选图片 URL')
    image_index: int = Field(default=0, ge=0, description='image_url 为空时，从当前页面自动候选列表中选择第几个，按大图优先排序')
    evidence: str = Field(default='', description='判断与关键词相关的证据')
    metadata: str = Field(default='', description='作者、时代、地点、分类、馆藏号等信息')
    summary: str = Field(default='', description='图片或藏品的简短中文说明')
    referer: str = Field(default='', description='可选 Referer；为空时使用 page_url')
    use_browser_cookies: bool = Field(default=True, description='Python 直连下载时是否附带当前浏览器会话 Cookie')
    prefer_browser_fetch: bool = Field(default=False, description='是否优先使用浏览器上下文 fetch；为空策略时默认先用 Python 直连，失败后浏览器 fetch')
    allow_clean_screenshot: bool = Field(default=True, description='直连和浏览器 fetch 都失败时，是否打开图片页并精确裁剪可见图片作为兜底')
    black_threshold: int = Field(default=18, ge=0, le=80, description='截图兜底自动去黑边阈值')
    white_threshold: int = Field(default=245, ge=180, le=255, description='截图兜底自动去白边阈值')
    border_ratio: float = Field(default=0.985, description='截图兜底一整行/列超过该比例为黑色或白色时才视为边框；工具会把异常值归一化到 0.90-0.999')
    allowed_host_suffixes: list[str] = Field(default_factory=list, description='可选域名后缀白名单，例如 ["idp.bl.uk", "data.idp.bl.uk"]；为空时允许任意公网 http(s) 图片 URL')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')
    timeout_seconds: int = Field(default=180, ge=30, le=900, description='直接下载超时时间')


class NavigateIdpSearchPageParams(BaseModel):
    """跳转 IDP 搜索结果页的参数模型"""
    keyword: str = Field(default='china temple', description='搜索关键词，默认 china temple')
    page: str | int = Field(default=1, description='页码；工具会从类似 2D 的脏值中提取数字')
    limit: str | int = Field(default=50, description='每页条数；工具会限制到 1-100')


class DownloadCurrentIdpSearchPageImagesParams(BaseModel):
    """批量下载当前 IDP 搜索结果页中的图片。"""
    target_count: int = Field(default=1000, ge=1, description='总目标有效记录数，达到后自动停止')
    max_items: int = Field(default=50, ge=1, le=100, description='当前搜索页最多处理多少个藏品结果')
    start_index: int = Field(default=0, ge=0, description='从当前搜索页第几个结果开始处理，0 表示第一个')
    images_per_item: int = Field(default=1, ge=1, le=5, description='每个藏品最多保存几张图片，默认只保存主图')
    file_prefix: str = Field(default='temple', description='保存文件名前缀，例如 temple')
    title_prefix: str = Field(default='china_temple', description='title.txt 中的标题前缀')
    allowed_host_suffixes: list[str] = Field(
        default_factory=lambda: ['idp.bl.uk', 'data.idp.bl.uk', 'bl.uk'],
        description='允许下载的官方域名后缀',
    )
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')
    timeout_seconds: int = Field(default=120, ge=30, le=900, description='Python 直接下载单张图片的超时时间')


class ValidateDownloadCompletionParams(BaseModel):
    """最终下载结果校验参数模型"""
    target_count: int = Field(default=100, ge=1, description='目标有效下载数量')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    title_filename: str = Field(default='title.txt', description='标题文件名')


class FinishDownloadTaskParams(BaseModel):
    """用确定性校验报告结束任务的参数模型"""
    target_count: int = Field(default=100, ge=1, description='目标有效下载数量')
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    title_filename: str = Field(default='title.txt', description='标题文件名')


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


# === 创建 tools 对象 ===
tools = Tools()
registry = tools.registry


# === 路径安全验证 ===

# 允许访问的基础目录（基于项目位置）
ALLOWED_BASE_DIRS = [
    PROJECT_DIR,
    RUN_DIR,
    Path(os.environ.get('BROWSER_USE_DOWNLOAD_DIR', str(Path.home() / 'Downloads'))),
]


def configure_runtime_paths(run_dir: Path, image_dir: Path | None = None, data_dir: Path | None = None) -> None:
    """
    配置本次运行的图片和数据目录。main.py 会在创建 ImagesCache 后调用。
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
    验证目标路径是否在允许的基础目录下，
    防止 LLM 通过工具参数读写任意文件。
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
    解析并校验提取工具的输入路径，不合法时回退到项目默认路径。
    """
    default_info_path = BASE_DIR / 'Information.md'
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
    为提取出的 Markdown/JSON/TXT 生成稳定安全的文件名，避免 agent 传入目录、空值或 Windows 特殊字符。
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
    读取 Information.md 并提取 HTML 代码块的首尾模式。
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
    把提取结果写入磁盘。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / file_name
    output_path.write_text(file_content, encoding='utf-8')
    return output_path


def _normalize_title(title: str, fallback: str = 'untitled') -> str:
    """
    清理写入 title.txt 的标题，保证每个标题只占一行。
    """
    normalized = title.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized or fallback


def _append_download_title(title: str, title_file: Path | None = None) -> Path:
    """
    将成功触发下载的图片标题追加写入真实的 browseruse_agent_data/title.txt。
    """
    target_file = title_file or AGENT_DATA_DIR / 'title.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)

    normalized_title = _normalize_title(title)
    existing_content = target_file.read_text(encoding='utf-8') if target_file.exists() else ''
    prefix = '' if not existing_content or existing_content.endswith('\n') else '\n'
    with open(target_file, 'a', encoding='utf-8') as f:
        f.write(f'{prefix}{normalized_title}\n')

    return target_file


def _safe_download_filename(title: str, url: str, suffix: str = '.tif') -> str:
    """
    根据标题生成稳定、安全的下载文件名。
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
    如果文件已存在，追加短 hash 避免覆盖。
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
    记录当前目录中已完成的 TIFF 文件，用于识别浏览器兜底下载产生的新文件。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    return {
        path: path.stat().st_mtime
        for pattern in ('*.tif', '*.tiff')
        for path in output_dir.glob(pattern)
        if path.is_file()
    }


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


async def _download_file(
    url: str,
    title: str,
    output_dir: Path | None = None,
    timeout_seconds: int = 180,
    referer: str | None = None,
    cookies: str | None = None,
) -> Path:
    """
    用 Python 直接下载文件，避免浏览器下载 tab / .crdownload / watchdog 状态采集干扰。
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
    从当前浏览器会话提取相关 URL 的 Cookie，供 Python 直接下载使用。
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


def _image_record_file(record_filename: str = 'image_record.jsonl') -> Path:
    """
    获取图片记录文件路径，并限制文件名只能落在 browseruse_agent_data 下。
    """
    normalized = re.sub(r'\s+', '', str(record_filename or '').strip())
    safe_name = Path(normalized).name
    if safe_name in {'', '.', 'image_record'} or not safe_name.endswith('.jsonl'):
        safe_name = 'image_record.jsonl'
    return AGENT_DATA_DIR / safe_name


def _max_downloaded_record_sequence(record_filename: str = 'image_record.jsonl') -> int:
    """
    返回已成功下载记录中的最大序号，避免 agent 传错 start_sequence 后覆盖旧图。
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
    返回 image 目录中同前缀文件名的最大数字序号。
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
    把文件名调整为 prefix_###，避免重复使用 agent 传入的旧序号。
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
    如果 agent 传入的序号已经用过或会覆盖文件，自动提升到安全序号。
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
    record_downloaded_image 接收的是已落地的临时文件。若临时文件本身就是本次请求序号
    （如 temple_001.jpg），不能再把它算作“已占用”而跳到 002。
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
    cleaned = re.sub(r'[\]\)},，。；;]+$', '', cleaned)
    return cleaned


def _sanitize_allowed_host_suffixes(suffixes: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in suffixes or []:
        text = str(raw or '').strip().strip('[](){}"\'`')
        for part in re.split(r'[,，\s]+', text):
            host = part.strip().strip('[]"\'`.').lower()
            if not host or '/' in host or ':' in host:
                continue
            if re.fullmatch(r'[a-z0-9.-]+', host) and host not in cleaned:
                cleaned.append(host)
    return cleaned


def _is_invalid_idp_collection_url(url: str) -> bool:
    parsed = urlparse(_clean_url_text(url))
    if (parsed.hostname or '').lower() != 'idp.bl.uk':
        return False
    path = parsed.path.strip('/')
    if path == 'collection' or path == 'collection/':
        return False
    if not path.startswith('collection/'):
        return False
    item_id = path.split('/', 1)[1].split('/', 1)[0].strip()
    return bool(item_id) and not re.fullmatch(r'[A-Fa-f0-9]{24,64}', item_id)


def _is_valid_idp_page_url(url: str) -> bool:
    parsed = urlparse(_clean_url_text(url))
    if (parsed.hostname or '').lower() != 'idp.bl.uk':
        return False
    path = parsed.path.strip('/')
    if path in {'collection', 'collection/'}:
        return True
    if path.startswith('collection/'):
        item_id = path.split('/', 1)[1].split('/', 1)[0].strip()
        return bool(re.fullmatch(r'[A-Fa-f0-9]{24,64}', item_id))
    return False


def _choose_reliable_page_url(agent_page_url: str, current_page_url: str) -> tuple[str, str]:
    agent_url = _clean_url_text(agent_page_url)
    current_url = _clean_url_text(current_page_url)
    if _is_invalid_idp_collection_url(agent_url):
        if _is_valid_idp_page_url(current_url):
            return current_url, f'- 已忽略模型传入的非法 IDP 详情页 URL，改用当前页面: {current_url}\n'
        return '', f'- 已拒绝模型传入的非法 IDP 详情页 URL: {agent_url}\n'
    return agent_url or current_url, ''


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


def _write_json_list(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_module.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')


def _safe_agent_data_filename(filename: str, fallback: str) -> str:
    """
    限制 agent 数据文件名只能落在 browseruse_agent_data 根目录，避免目录穿越。
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


def _idp_manifest_url_from_page_url(page_url: str) -> str:
    parsed = urlparse((page_url or '').strip())
    host = (parsed.hostname or '').lower()
    path = parsed.path.strip('/')
    if host == 'idp.bl.uk' and path.startswith('collection/'):
        item_id = path.split('/', 1)[1].split('/', 1)[0].strip()
        if re.fullmatch(r'[A-Fa-f0-9]{24,64}', item_id):
            return f'https://data.idp.bl.uk/iiif/3/manifest/{item_id.upper()}'
    if host == 'data.idp.bl.uk' and '/iiif/3/manifest/' in f'/{path}/':
        return (page_url or '').strip().rstrip('/')
    return ''


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
    转义 Markdown 表格单元格，保持记录文件可解析。
    """
    text = str(value or '').replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    text = text.replace('|', '\\|')
    return re.sub(r'\s+', ' ', text).strip()


def _rewrite_image_title_file(data_dir: Path, records: list[dict]) -> Path:
    title_file = data_dir / 'title.txt'
    titles = [
        _normalize_title(str(record.get('title') or record.get('collection_title') or record.get('file_name') or 'untitled'))
        for record in sorted(records, key=_record_sort_key)
        if record.get('status') == 'downloaded'
    ]
    title_file.write_text('\n'.join([*titles, 'END', '']), encoding='utf-8')
    return title_file


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
    将保存文件名解析到项目 image 目录；只接受文件名，禁止目录穿越。
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
    for raw_url in (page_url, image_url):
        parsed = urlparse((raw_url or '').strip())
        path_parts = [part for part in parsed.path.split('/') if part]
        host = (parsed.hostname or '').lower()
        if host == 'idp.bl.uk' and len(path_parts) >= 2 and path_parts[0] == 'collection':
            return path_parts[1].upper()
        if host == 'data.idp.bl.uk' and 'manifest' in path_parts:
            index = path_parts.index('manifest')
            if index + 1 < len(path_parts):
                return path_parts[index + 1].upper()
        if host == 'data.idp.bl.uk' and 'iiif' in path_parts:
            try:
                index = path_parts.index('3')
            except ValueError:
                continue
            if index + 1 < len(path_parts):
                return path_parts[index + 1].upper()
    return ''


def _final_image_filename(title: str, sequence: int, file_hash: str, suffix: str) -> str:
    normalized_title = _normalize_title(title, fallback=f'image_{sequence:03d}')
    normalized_title = re.sub(r'^(?:temple|image)_\d{1,6}_?', '', normalized_title, flags=re.IGNORECASE)
    if not re.search(r'_\d{3,}(?:_|$)', normalized_title):
        normalized_title = f'{sequence:03d}_{normalized_title}'
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', normalized_title)
    safe_stem = re.sub(r'_+', '_', safe_stem).strip('._ ')[:180] or f'image_{sequence:03d}'
    short_hash = (file_hash or '')[:8] or 'nohash'
    if not safe_stem.endswith(f'_{short_hash}'):
        safe_stem = f'{safe_stem}_{short_hash}'
    return f'{safe_stem}{normalize_image_ext(suffix, fallback=".jpg")}'


def _rename_image_to_final_name(image_path: Path, title: str, sequence: int, file_hash: str) -> Path:
    final_name = _final_image_filename(title, sequence, file_hash, image_path.suffix)
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


def _build_existing_image_hash_index() -> dict[str, Path]:
    """
    Scan IMAGE_DIR once per batch so duplicate orphan files do not require
    re-hashing the whole directory for every new image.
    """
    existing: dict[str, Path] = {}
    if not IMAGE_DIR.exists():
        return existing
    for image_path in sorted(IMAGE_DIR.iterdir(), key=lambda path: path.name.lower()):
        if not image_path.is_file() or image_path.name == 'rename_record.txt':
            continue
        if normalize_image_ext(image_path.suffix, fallback='') not in EXT_TO_PIL_FORMAT:
            continue
        try:
            existing.setdefault(_sha256_file(image_path), image_path)
        except OSError:
            continue
    return existing


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


def _append_image_title_record(data_dir: Path, record: dict) -> Path:
    title = _normalize_title(str(record.get('title') or record.get('collection_title') or record.get('file_name') or 'untitled'))
    return _append_download_title(title, data_dir / 'title.txt')


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
) -> ActionResult:
    """
    Fast batch-only record path: uses in-memory indexes and append-only writes.
    It intentionally avoids record_downloaded_image(), which reloads JSONL and
    rescans IMAGE_DIR on every call.
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

        image_path = _rename_image_to_final_name(image_path, normalized_title, sequence, file_hash)
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
        title_file = _append_image_title_record(data_dir, record)
        info_file = _append_image_info_record(data_dir, record, info_filename)

        return ActionResult(
            extracted_content=(
                f'✅ 已快速记录图片 #{sequence}: {image_path.name}\n'
                f'- content_hash: {file_hash}\n'
                f'- source_hash: {source_hash}\n'
                f'- 当前有效记录: {record_index.downloaded_count}\n'
                f'- 标题文件: {title_file}\n'
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
    在浏览器页面上下文中 fetch 图片，等价于使用当前浏览器会话/权限获取图片资源。
    这比驱动原生右键菜单稳定，也能使用站点 Cookie。
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
    只裁掉几乎整行/整列都是黑色或白色的外边框，避免误裁作品本身的深色/浅色区域。
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


async def _detect_human_verification(browser_session) -> dict:
    """
    检测当前页面是否处于 Cloudflare / 人机验证页。
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


# === 注册自定义动作 ===

@tools.action(
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


@tools.action(
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


@tools.action(
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


@tools.action(
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


@tools.action(
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


@tools.action(
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


@tools.action(
    description='检测当前页面是否为 Cloudflare/人机验证页；如果是，则等待用户在浏览器中手动完成验证后再继续。不会自动点击或绕过验证码。',
    param_model=WaitForHumanVerificationParams,
)
async def wait_for_human_verification(params: WaitForHumanVerificationParams, browser_session):
    """
    人机验证必须由用户手动完成；本工具只负责检测和等待，避免 agent 继续误操作。
    """
    try:
        deadline = asyncio.get_running_loop().time() + params.timeout_seconds
        first_state = await _detect_human_verification(browser_session)
        if not first_state.get('is_challenge'):
            msg = '✅ 当前页面未检测到 Cloudflare/人机验证，可以继续执行。'
            return ActionResult(extracted_content=msg, include_in_memory=True, long_term_memory='当前页面未检测到人机验证')

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(params.poll_interval_seconds)
            state = await _detect_human_verification(browser_session)
            if not state.get('is_challenge'):
                msg = (
                    '✅ 人机验证已完成，页面已恢复，可以继续处理队列。\n'
                    f"当前页面: {state.get('url', '')}\n"
                    f"标题: {state.get('title', '')}"
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory='人机验证已由用户手动完成，可以继续任务',
                )

        msg = (
            '仍处于 Cloudflare/人机验证页面。请在打开的浏览器中手动点击验证按钮并等待页面加载完成，'
            '然后再次调用 wait_for_human_verification 或继续当前队列项。\n'
            f"页面: {first_state.get('url', '')}\n"
            f"标题: {first_state.get('title', '')}\n"
            f"页面文本: {first_state.get('text_sample', '')}"
        )
        return ActionResult(error=msg)
    except Exception as e:
        return ActionResult(error=f'等待人机验证时出错: {str(e)}')


@tools.action(
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


@tools.action(
    description=(
        '记录一张已成功保存到 image 目录的非 LOC 图片。'
        '工具会用 UTF-8 自动去重并重写 browseruse_agent_data/image_record.jsonl、title.txt 和信息表，'
        '避免 write_file 追加导致重复行或 GBK 编码错误。'
    ),
    param_model=RecordDownloadedImageParams,
)
async def record_downloaded_image(params: RecordDownloadedImageParams):
    """
    为普通网站/截图下载流程记录图片、标题和元数据。
    """
    try:
        data_dir = AGENT_DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)

        image_path = _record_image_file_path(params.file_name)
        if not image_path.exists() or not image_path.is_file():
            return ActionResult(error=f'图片文件不存在，不能记录: {image_path}')
        try:
            _validate_saved_image_file(image_path, source='record_downloaded_image')
        except RuntimeError as exc:
            return ActionResult(error=f'图片质量校验失败，拒绝记录: {exc}')

        sequence, sequence_note = _safe_record_sequence_for_existing_file(
            params.sequence,
            params.record_filename,
            _prefix_from_filename(params.file_name, 'temple'),
            image_path,
        )
        file_hash = _sha256_file(image_path)
        normalized_title = _normalize_title(params.title, fallback=image_path.stem)
        source_hash = _source_hash(params.page_url, params.image_url, 0)
        record_file = data_dir / _safe_agent_data_filename(params.record_filename, 'image_record.jsonl')
        records = _load_image_records(record_file)
        for record in records:
            if record.get('status') != 'downloaded':
                continue
            if _record_sequence(record) == sequence:
                return ActionResult(error=f'序号 #{params.sequence} 已有记录，拒绝覆盖旧记录: {record.get("file_name", "")}')
            if Path(str(record.get('file_name') or '')).name == image_path.name:
                return ActionResult(error=f'文件名已被记录，拒绝覆盖旧记录: {image_path.name}')
            if source_hash and str(record.get('source_hash') or '').strip() == source_hash:
                image_path.unlink(missing_ok=True)
                msg = (
                    f'✅ 来源已处理，已删除本次重复文件并跳过\n'
                    f'- 已有序号: {record.get("sequence")}\n'
                    f'- 已有文件: {record.get("file_name", "")}\n'
                    f'- source_hash: {source_hash}'
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory=f'来源已记录，跳过重复记录: {record.get("file_name", "")}',
                )
            if params.image_url.strip() and str(record.get('image_url') or '').strip() == params.image_url.strip():
                image_path.unlink(missing_ok=True)
                msg = (
                    f'✅ 图片 URL 已有下载记录，已删除本次重复文件并跳过\n'
                    f'- 已有序号: {record.get("sequence")}\n'
                    f'- 已有文件: {record.get("file_name", "")}\n'
                    f'- 图片 URL: {params.image_url.strip()}'
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory=f'图片 URL 已记录，跳过重复记录: {record.get("file_name", "")}',
                )
            if _record_file_sha256(record) == file_hash:
                image_path.unlink(missing_ok=True)
                msg = (
                    f'✅ 图片内容已有下载记录，已删除本次重复文件并跳过\n'
                    f'- 已有序号: {record.get("sequence")}\n'
                    f'- 已有文件: {record.get("file_name", "")}\n'
                    f'- SHA256: {file_hash}'
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory=f'图片内容已记录，跳过重复记录: {record.get("file_name", "")}',
                )

        existing_image_path = _find_existing_image_file_by_hash(file_hash, exclude_path=image_path)
        if existing_image_path:
            image_path.unlink(missing_ok=True)
            msg = (
                f'✅ image 目录中已存在相同图片内容，已删除本次重复文件并跳过\n'
                f'- 已有文件: {existing_image_path.name}\n'
                f'- 本次文件: {image_path.name}\n'
                f'- SHA256: {file_hash}'
            )
            return ActionResult(
                extracted_content=msg,
                include_in_memory=True,
                long_term_memory=f'image 目录已有相同图片，跳过重复记录: {existing_image_path.name}',
            )

        image_path = _rename_image_to_final_name(image_path, normalized_title, sequence, file_hash)
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
            'source_item_id': _source_item_id_from_urls(params.page_url, params.image_url),
            'title_hash': _hash_text(normalized_title, 'sha1'),
            'title': normalized_title,
            'collection_title': _normalize_title(params.collection_title, fallback=params.title),
            'page_url': params.page_url.strip(),
            'image_url': params.image_url.strip(),
            'evidence': params.evidence.strip(),
            'metadata': params.metadata.strip(),
            'summary': params.summary.strip(),
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        }
        records.append(record)
        records.sort(key=_record_sort_key)

        _write_image_records(record_file, records)
        title_file = _rewrite_image_title_file(data_dir, records)
        info_file = _rewrite_image_info_file(data_dir, records, params.info_filename)

        downloaded_count = sum(1 for item in records if item.get('status') == 'downloaded')
        msg = (
            f'✅ 已最终命名并记录图片 #{sequence}: {image_path.name}\n'
            f'{sequence_note + chr(10) if sequence_note else ""}'
            f'- content_hash: {file_hash}\n'
            f'- source_hash: {source_hash}\n'
            f'- 当前有效记录: {downloaded_count}\n'
            f'- 标题文件: {title_file}\n'
            f'- 信息表: {info_file}\n'
            f'- 结构化记录: {record_file}'
        )
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'已记录图片 #{params.sequence} {image_path.name}，当前共 {downloaded_count} 条有效记录',
        )
    except Exception as e:
        return ActionResult(error=f'记录图片时出错: {str(e)}')


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
        raise RuntimeError('当前页面未找到可下载的公网图片 URL；如页面已显示大图，可允许 clean_screenshot 兜底。')
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


def _count_title_lines(title_file: Path) -> int:
    if not title_file.exists():
        return 0
    return len([
        line for line in title_file.read_text(encoding='utf-8').splitlines()
        if line.strip() and line.strip().upper() != 'END'
    ])


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
    title_filename: str = 'title.txt',
    *,
    validate_image_files: bool = True,
    include_duplicate_hash_groups: bool = True,
) -> dict:
    data_dir = AGENT_DATA_DIR
    record_file = data_dir / _safe_agent_data_filename(record_filename, 'image_record.jsonl')
    title_file = data_dir / _safe_agent_data_filename(title_filename, 'title.txt')
    records = _read_downloaded_records(record_file)
    title_count = _count_title_lines(title_file)
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
        and title_count >= target_count
        and len(image_files) >= target_count
        and (not validate_image_files or not bad_files)
        and (not include_duplicate_hash_groups or not duplicate_hash_groups)
    )
    remaining_records = max(0, target_count - len(records))
    return {
        'complete': complete,
        'target_count': target_count,
        'downloaded_records': len(records),
        'title_count': title_count,
        'image_file_count': len(image_files),
        'remaining_records': remaining_records,
        'missing_sequences': missing_sequences,
        'bad_files': bad_files,
        'orphan_files': orphan_files,
        'duplicate_hash_groups': duplicate_hash_groups,
        'validate_image_files': validate_image_files,
        'include_duplicate_hash_groups': include_duplicate_hash_groups,
        'record_file': str(record_file),
        'title_file': str(title_file),
    }


def format_download_validation_report(validation: dict) -> str:
    status = 'SUCCESS' if validation.get('complete') else 'INCOMPLETE'
    lines = [
        f'Final download validation: {status}',
        f"- target_count: {validation.get('target_count')}",
        f"- downloaded_records: {validation.get('downloaded_records')}",
        f"- remaining_records_needed: {validation.get('remaining_records')}",
        f"- title_txt_entries: {validation.get('title_count')}",
        f"- image_files: {validation.get('image_file_count')}",
        f"- sequence_gaps_warning_only: {validation.get('missing_sequences') or 'none'}",
        f"- bad_or_empty_images: {len(validation.get('bad_files') or [])}",
        f"- duplicate_image_hash_groups: {len(validation.get('duplicate_hash_groups') or [])}",
        f"- image_file_validation: {'enabled' if validation.get('validate_image_files') else 'skipped_for_batch'}",
        f"- duplicate_hash_scan: {'enabled' if validation.get('include_duplicate_hash_groups') else 'skipped_for_batch'}",
        f"- orphan_files_warning_only: {len(validation.get('orphan_files') or [])}",
        f"- record_file: {validation.get('record_file')}",
        f"- title_file: {validation.get('title_file')}",
    ]
    if validation.get('bad_files'):
        lines.append('- bad_image_details:')
        lines.extend(f"  - {item['file']}: {item['error']}" for item in validation['bad_files'][:20])
    if validation.get('orphan_files'):
        lines.append('- orphan_files_first_20: ' + ', '.join(validation['orphan_files'][:20]))
    return '\n'.join(lines)


@tools.action(
    description='最终校验下载结果，只根据 image_record.jsonl、title.txt 和 image 目录生成确定性报告；done 前必须先调用它，不要让 agent 自己编统计。',
    param_model=ValidateDownloadCompletionParams,
)
async def validate_download_completion(params: ValidateDownloadCompletionParams):
    validation = validate_download_artifacts(
        target_count=params.target_count,
        record_filename=params.record_filename,
        title_filename=params.title_filename,
        validate_image_files=True,
        include_duplicate_hash_groups=True,
    )
    report = format_download_validation_report(validation)
    report_file = AGENT_DATA_DIR / 'final_download_report.md'
    report_file.write_text(report + '\n', encoding='utf-8')
    return ActionResult(
        extracted_content=report,
        include_in_memory=True,
        long_term_memory=(
            'Final validation passed; finish_download_task may end with success=True'
            if validation['complete']
            else f'Final validation incomplete; need {validation["remaining_records"]} more valid records; do not finish yet'
        ),
    )


@tools.action(
    description=(
        '用本地文件的确定性校验报告结束任务；不要再调用内置 done。'
        '校验 SUCCESS 时返回 success=True；否则返回 success=False，最终文本只包含程序报告，避免 LLM 自行扩写乱码。'
    ),
    param_model=FinishDownloadTaskParams,
)
async def finish_download_task(params: FinishDownloadTaskParams):
    validation = validate_download_artifacts(
        target_count=params.target_count,
        record_filename=params.record_filename,
        title_filename=params.title_filename,
        validate_image_files=True,
        include_duplicate_hash_groups=True,
    )
    report = format_download_validation_report(validation)
    report_file = AGENT_DATA_DIR / 'final_download_report.md'
    report_file.write_text(report + '\n', encoding='utf-8')
    return ActionResult(
        is_done=True,
        success=bool(validation['complete']),
        extracted_content=report,
        long_term_memory=(
            'Task finished with deterministic validation SUCCESS'
            if validation['complete']
            else f'Task finished with deterministic validation INCOMPLETE; need {validation["remaining_records"]} more valid records'
        ),
    )


@tools.action(
    description='生成并跳转到 IDP 官方搜索结果页，避免 agent 手拼 URL 时把 page=21 写成 page=2D、term 写成 china%2Otemple 等脏参数。',
    param_model=NavigateIdpSearchPageParams,
)
async def navigate_idp_search_page(params: NavigateIdpSearchPageParams, browser_session):
    keyword = re.sub(r'\s+', ' ', str(params.keyword or 'china temple')).strip() or 'china temple'
    page = _coerce_int(params.page, default=1, minimum=1, maximum=999)
    limit = _coerce_int(params.limit, default=50, minimum=1, maximum=100)
    url = 'https://idp.bl.uk/collection/?' + urlencode({'term': keyword, 'limit': limit, 'page': page})
    await _navigate_to_image_url(browser_session, url)
    _write_idp_progress({
        **_load_idp_progress(),
        'keyword': keyword,
        'current_page': page,
        'next_page': page,
        'next_index': 0,
        'limit': limit,
        'last_search_url': url,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    return ActionResult(
        extracted_content=(
            f'✅ 已跳转到 IDP 搜索结果页\n'
            f'- keyword: {keyword}\n'
            f'- page: {page}\n'
            f'- limit: {limit}\n'
            f'- url: {url}'
        ),
        include_in_memory=True,
        long_term_memory=f'IDP 搜索页已跳转: page={page}, keyword={keyword}',
    )


def _idp_progress_file() -> Path:
    return AGENT_DATA_DIR / 'idp_progress.json'


def _load_idp_progress() -> dict:
    progress_file = _idp_progress_file()
    if not progress_file.exists():
        return {}
    try:
        data = json_module.loads(progress_file.read_text(encoding='utf-8'))
    except json_module.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_idp_progress(progress: dict) -> Path:
    progress_file = _idp_progress_file()
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(json_module.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
    return progress_file


def _current_idp_search_page_from_url(url: str) -> tuple[str, int, int]:
    parsed = urlparse(url or '')
    query = parse_qs(parsed.query)
    keyword = (query.get('term') or ['china temple'])[0] or 'china temple'
    try:
        page = max(1, int((query.get('page') or ['1'])[0]))
    except ValueError:
        page = 1
    try:
        limit = min(max(1, int((query.get('limit') or ['50'])[0])), 100)
    except ValueError:
        limit = 50
    return keyword or 'china temple', page, limit


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


def _idp_page_progress_state(page: int) -> dict | None:
    progress = load_page_progress(AGENT_DATA_DIR)
    for item in progress.get('pages') or []:
        try:
            if int(item.get('page')) == page:
                return item if isinstance(item, dict) else None
        except (TypeError, ValueError):
            continue
    return None


def _effective_idp_start_index(requested_start_index: int, page: int) -> tuple[int, str]:
    """
    Do not let an agent-provided start_index=0 move a page backwards after
    idp_page_progress.json has already advanced it.
    """
    try:
        requested = max(0, int(requested_start_index))
    except (TypeError, ValueError):
        requested = 0
    state = _idp_page_progress_state(page)
    if not state:
        return requested, ''
    try:
        progress_index = max(0, int(state.get('next_index') or 0))
    except (TypeError, ValueError):
        progress_index = 0
    effective = max(requested, progress_index)
    if effective != requested:
        return effective, f'已根据 idp_page_progress.json 将 start_index 从 {requested} 提升到 {effective}'
    return effective, ''


def _idp_batch_item_cap(requested_max_items: int) -> int:
    raw_value = os.environ.get('BROWSER_USE_IDP_BATCH_ITEM_CAP', '').strip()
    if not raw_value:
        return requested_max_items
    try:
        return min(100, max(1, int(raw_value)))
    except ValueError:
        return requested_max_items


def _record_idp_empty_page_event(page_url: str, page: int, start_index: int, total_found: int, note: str) -> Path:
    event_file = AGENT_DATA_DIR / 'idp_empty_page_events.jsonl'
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


async def _extract_current_idp_search_items(browser_session, max_items: int, start_index: int) -> dict:
    js_code = '''
    (function() {
        try {
            const seen = new Set();
            const items = [];
            const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            for (const link of document.querySelectorAll('a[href*="/collection/"]')) {
                const href = link.href || link.getAttribute('href') || '';
                let url;
                try {
                    url = new URL(href, window.location.href).href;
                } catch (_) {
                    continue;
                }
                const match = url.match(/\\/collection\\/([A-Fa-f0-9]{24,64})\\/?/);
                if (!match) continue;
                const id = match[1].toUpperCase();
                if (seen.has(id)) continue;
                seen.add(id);
                const container = link.closest('article, li, .card, .result, .collection-item, div') || link;
                const title = clean(
                    link.getAttribute('title') ||
                    link.textContent ||
                    (container && container.textContent) ||
                    id
                );
                items.push({
                    id,
                    url: `https://idp.bl.uk/collection/${id}/`,
                    title,
                    manifest_url: `https://data.idp.bl.uk/iiif/3/manifest/${id}`,
                });
            }
            return {
                success: true,
                page_url: window.location.href,
                page_title: document.title,
                total_found: items.length,
                body_text_length: (document.body && document.body.innerText || '').trim().length,
                anchor_count: document.querySelectorAll('a').length,
                collection_link_count: document.querySelectorAll('a[href*="/collection/"]').length,
                items: items.slice(''' + json_module.dumps(start_index) + ''', ''' + json_module.dumps(start_index + max_items) + '''),
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
        raise RuntimeError(result['exceptionDetails'].get('text', 'IDP 搜索结果提取失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', 'IDP 搜索结果提取失败'))
    data['items'] = [item for item in data.get('items') or [] if isinstance(item, dict)]
    return data


async def _fetch_idp_manifest_summary_in_browser(browser_session, manifest_url: str) -> dict:
    js_code = '''
    (async function(manifestUrl) {
        const textValue = (value) => {
            if (!value) return '';
            if (typeof value === 'string') return value;
            if (Array.isArray(value)) return value.map(textValue).filter(Boolean).join(' ');
            if (typeof value === 'object') {
                if (Array.isArray(value.en)) return value.en.join(' ');
                if (Array.isArray(value.none)) return value.none.join(' ');
                const first = Object.values(value).find((item) => Array.isArray(item) && item.length);
                if (first) return first.join(' ');
            }
            return String(value || '');
        };
        const addUrl = (urls, seen, raw) => {
            if (!raw || typeof raw !== 'string') return;
            let url = raw.trim();
            if (!/^https?:\\/\\//i.test(url)) return;
            if (/\\/image\\/iiif\\/3\\/[^/]+$/i.test(new URL(url).pathname)) {
                url = url.replace(/\\/$/, '') + '/full/max/0/default.jpg';
            }
            if (!seen.has(url)) {
                seen.add(url);
                urls.push(url);
            }
        };
        const walk = (node, urls, seen) => {
            if (!node) return;
            if (Array.isArray(node)) {
                for (const child of node) walk(child, urls, seen);
                return;
            }
            if (typeof node !== 'object') return;
            const id = node.id || node['@id'];
            if (typeof id === 'string' && (/\\/image\\/iiif\\//i.test(id) || /\\/mediaLib\\//i.test(id))) {
                addUrl(urls, seen, id);
            }
            const service = node.service || node.services;
            if (service) walk(service, urls, seen);
            for (const child of Object.values(node)) walk(child, urls, seen);
        };
        const score = (url) => {
            let value = 0;
            if (/\\/image\\/iiif\\//i.test(url)) value += 500;
            if (/\\/full\\/(?:max|full)\\//i.test(url)) value += 220;
            if (/default\\.(?:jpe?g|png|webp|tiff?)(?:[?#]|$)/i.test(url)) value += 80;
            if (/\\/mediaLib\\//i.test(url)) value += 60;
            if (/thumb|thumbnail|small|icon|logo|sprite/i.test(url)) value -= 300;
            return value;
        };
        try {
            const response = await fetch(manifestUrl, {
                credentials: 'include',
                cache: 'no-store',
                headers: {Accept: 'application/json,application/ld+json;q=0.9,*/*;q=0.8'},
            });
            if (!response.ok) {
                return {success: false, error: `HTTP ${response.status}: ${response.statusText}`, manifest_url: manifestUrl};
            }
            const manifest = await response.json();
            const urls = [];
            const seen = new Set();
            walk(manifest.items || manifest.sequences || manifest, urls, seen);
            urls.sort((a, b) => score(b) - score(a) || a.localeCompare(b));
            const metadata = Array.isArray(manifest.metadata)
                ? manifest.metadata.map((item) => `${textValue(item.label)}: ${textValue(item.value)}`).filter(Boolean).join('; ')
                : '';
            return {
                success: true,
                manifest_url: manifestUrl,
                label: textValue(manifest.label),
                summary: textValue(manifest.summary || manifest.description),
                metadata,
                image_urls: urls,
            };
        } catch (error) {
            return {success: false, error: error.message, manifest_url: manifestUrl};
        }
    })(''' + json_module.dumps(manifest_url, ensure_ascii=False) + ''')
    '''
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', 'IDP manifest 解析失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', 'IDP manifest 解析失败'))
    return data


@tools.action(
    description=(
        '批量处理当前 IDP 搜索结果页：一次提取多个 /collection/ 藏品，'
        '在浏览器上下文 fetch IIIF manifest 和图片，下载到 image 目录，并写入 image_record.jsonl/title.txt。'
        '用于替代 agent 每张图逐页点击，可显著提升 china temple 这类 IDP 批量任务效率。'
    ),
    param_model=DownloadCurrentIdpSearchPageImagesParams,
)
async def download_current_idp_search_page_images(params: DownloadCurrentIdpSearchPageImagesParams, browser_session):
    try:
        record_index = _build_download_record_index(params.record_filename)
        existing_image_hashes = _build_existing_image_hash_index()
        next_sequence = max(record_index.max_sequence, _max_image_file_sequence(params.file_prefix)) + 1
        remaining = max(0, params.target_count - record_index.downloaded_count)
        if remaining == 0:
            report = format_download_validation_report(validate_download_artifacts(
                target_count=params.target_count,
                record_filename=params.record_filename,
            ))
            return ActionResult(extracted_content='✅ 已达到目标数量，无需继续下载。\n' + report, include_in_memory=True)

        allowed_host_suffixes = _sanitize_allowed_host_suffixes(params.allowed_host_suffixes)
        requested_max_items = min(params.max_items, max(1, remaining))
        batch_item_cap = _idp_batch_item_cap(requested_max_items)
        max_items = min(requested_max_items, batch_item_cap)
        current_url = await _current_browser_url(browser_session)
        _, current_page_from_url, _ = _current_idp_search_page_from_url(current_url)
        effective_start_index, start_index_note = _effective_idp_start_index(params.start_index, current_page_from_url)
        page_data = await _extract_current_idp_search_items(
            browser_session,
            max_items=max_items,
            start_index=effective_start_index,
        )
        items = page_data.get('items') or []
        if not items:
            page_url_for_retry = str(page_data.get('page_url') or current_url)
            keyword_for_retry, page_for_retry, limit_for_retry = _current_idp_search_page_from_url(page_url_for_retry)
            if 'idp.bl.uk/collection/' in page_url_for_retry or 'idp.bl.uk/collection?' in page_url_for_retry:
                await _navigate_to_image_url(browser_session, page_url_for_retry)
                await asyncio.sleep(3)
                page_data = await _extract_current_idp_search_items(
                    browser_session,
                    max_items=max_items,
                    start_index=effective_start_index,
                )
                items = page_data.get('items') or []
            if not items:
                event_file = _record_idp_empty_page_event(
                    str(page_data.get('page_url') or page_url_for_retry),
                    page_for_retry,
                    effective_start_index,
                    int(page_data.get('total_found') or 0),
                    'no_collection_items_after_reload',
                )
                _write_idp_progress({
                    **_load_idp_progress(),
                    'keyword': keyword_for_retry,
                    'current_page': page_for_retry,
                    'next_page': page_for_retry,
                    'next_index': effective_start_index,
                    'limit': limit_for_retry,
                    'last_error': 'empty_idp_search_page_after_reload',
                    'empty_page_events_file': str(event_file),
                    'updated_at': datetime.now(timezone.utc).isoformat(),
                })
                return ActionResult(
                    error=(
                        '当前 IDP 搜索页未提取到 /collection/ 结果，刷新后仍为空；'
                        '这通常是 IDP SPA 初始化失败或浏览器会话变脏。'
                        f' page={page_for_retry}, start_index={effective_start_index}, '
                        f'body_text_length={page_data.get("body_text_length")}, '
                        f'anchor_count={page_data.get("anchor_count")}, '
                        f'collection_link_count={page_data.get("collection_link_count")}. '
                        '请重启浏览器会话后从 idp_page_progress.json 续跑。'
                    )
                )
        if not items:
            return ActionResult(error='当前页面未提取到 IDP /collection/ 搜索结果；请先调用 navigate_idp_search_page 打开搜索结果页。')
        keyword, current_page, page_limit = _current_idp_search_page_from_url(str(page_data.get('page_url') or ''))

        downloaded: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        processed_items = 0

        async with DOWNLOAD_LOCK:
            for item in items:
                if record_index.downloaded_count >= params.target_count:
                    break
                processed_items += 1
                manifest_url = str(item.get('manifest_url') or '')
                page_url = str(item.get('url') or '')
                try:
                    manifest = await _fetch_idp_manifest_summary_in_browser(browser_session, manifest_url)
                except Exception as exc:
                    errors.append(f'{page_url}: manifest 解析失败: {exc}')
                    continue

                image_urls = []
                for raw_url in manifest.get('image_urls') or []:
                    try:
                        image_urls.append(_validate_public_image_url(str(raw_url), allowed_host_suffixes))
                    except RuntimeError:
                        continue
                if not image_urls:
                    skipped.append(f'{page_url}: manifest 未找到可下载图片')
                    continue

                saved_for_item = 0
                for image_url in image_urls:
                    if saved_for_item >= params.images_per_item:
                        break
                    if record_index.downloaded_count >= params.target_count:
                        break
                    existing_record = record_index.records_by_image_url.get(image_url)
                    if existing_record:
                        skipped.append(f'{page_url}: 图片 URL 已记录 #{existing_record.get("sequence")}')
                        continue

                    while next_sequence in record_index.used_sequences:
                        next_sequence += 1
                    sequence = next_sequence
                    file_name = f'{params.file_prefix}_{sequence:03d}'
                    label = _normalize_title(str(manifest.get('label') or item.get('title') or item.get('id') or 'IDP item'))
                    title = _normalize_title(f'{params.title_prefix}_{sequence:03d}_{label}')

                    try:
                        try:
                            image_path = await _download_image_to_file(
                                image_url,
                                file_name,
                                params.timeout_seconds,
                                referer=page_url,
                                cookies=None,
                            )
                            download_method = 'python_direct'
                        except Exception as direct_exc:
                            image_path = await _browser_fetch_image_to_file(browser_session, image_url, file_name)
                            download_method = f'browser_context_fetch_after_python_error:{direct_exc}'
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

                        evidence_keyword = keyword or params.title_prefix or 'IDP'
                        record_result = await _record_saved_image_fast(
                            image_path=image_path,
                            sequence=sequence,
                            title=title,
                            collection_title=label,
                            page_url=page_url,
                            image_url=image_url,
                            evidence=f'IDP search result for {evidence_keyword}; image URL resolved from official IIIF manifest.',
                            metadata=str(manifest.get('metadata') or '').strip() or '未显示',
                            summary=str(manifest.get('summary') or label or 'IDP official collection image').strip(),
                            record_filename=params.record_filename,
                            info_filename=params.info_filename,
                            record_index=record_index,
                            existing_image_hashes=existing_image_hashes,
                        )
                        if record_result.error:
                            image_path.unlink(missing_ok=True)
                            errors.append(f'{page_url}: 记录失败: {record_result.error}')
                            continue
                        _record_generic_image_method_success(download_method.split(':', 1)[0], sequence, image_url)
                        recorded_file_name = str((record_index.records_by_image_url.get(image_url) or {}).get('file_name') or image_path.name)
                        downloaded.append(f'#{sequence}: {recorded_file_name} | {label} | {page_url} | {download_method}')
                        next_sequence = max(next_sequence + 1, record_index.max_sequence + 1)
                        saved_for_item += 1
                    except Exception as exc:
                        _record_generic_image_method_failure('browser_context_fetch', sequence, image_url, str(exc))
                        errors.append(f'{page_url}: 图片下载失败: {exc}')

        validation = validate_download_artifacts(
            target_count=params.target_count,
            record_filename=params.record_filename,
            validate_image_files=False,
            include_duplicate_hash_groups=False,
        )
        report = format_download_validation_report(validation)
        report_file = AGENT_DATA_DIR / 'final_download_report.md'
        report_file.write_text(report + '\n', encoding='utf-8')
        total_found = int(page_data.get('total_found') or 0)
        next_index = effective_start_index + processed_items
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
            start_index=effective_start_index,
            processed_items=processed_items,
            downloaded_count=len(downloaded),
            skipped_count=len(skipped),
            error_count=len(errors),
            total_found=total_found,
            last_error='; '.join(errors[:3]),
        )
        progress_file = _write_idp_progress({
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
            'last_search_url': page_data.get('page_url', ''),
            'page_progress_file': str(AGENT_DATA_DIR / 'idp_page_progress.json'),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })

        msg = (
            '✅ IDP 当前搜索页批量处理完成\n'
            f'- 处理藏品数: {processed_items}/{len(items)}\n'
            f'- 本次新增下载: {len(downloaded)}\n'
            f'- 跳过: {len(skipped)}\n'
            f'- 错误: {len(errors)}\n'
            f'- 当前有效记录: {validation["downloaded_records"]}/{params.target_count}\n'
            f'- 进度文件: {progress_file}\n'
            f'- 下次建议: page={active_page["page"]}, start_index={active_page["next_index"]}\n'
        )
        if requested_max_items != max_items:
            msg += f'- 批量上限: agent 请求 max_items={requested_max_items}，已按 BROWSER_USE_IDP_BATCH_ITEM_CAP 限制为 {max_items}\n'
        if start_index_note:
            msg += f'- start_index 修正: {start_index_note}\n'
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
            long_term_memory=f'IDP 批量下载新增 {len(downloaded)} 张，当前 {validation["downloaded_records"]}/{params.target_count}',
        )
    except Exception as e:
        return ActionResult(error=f'IDP 当前搜索页批量下载时出错: {str(e)}')


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

        if _is_invalid_idp_collection_url(page_url):
            page_url, page_url_note = _choose_reliable_page_url(page_url, '')
        if not page_url and _is_invalid_idp_collection_url(params.page_url) and not params.image_url.strip():
            return ActionResult(error=f'模型传入的 IDP 详情页 URL 非法，且无法从浏览器获取可信当前页: {params.page_url}')

        manifest_url_from_page = _idp_manifest_url_from_page_url(page_url)
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
            manifest_note += f'- 已从 IDP 详情页 URL 推导 IIIF manifest: {manifest_url_from_page}\n'

        if image_url and _looks_like_iiif_manifest_url(image_url):
            cookie_header = ''
            if params.use_browser_cookies:
                try:
                    cookie_header = await _get_browser_cookie_header(browser_session, [image_url, page_url])
                except Exception:
                    cookie_header = ''
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

        existing_record = _find_downloaded_record_by_image_url(params.record_filename, image_url)
        if existing_record:
            msg = (
                f'✅ 图片 URL 已有下载记录，视为当前图片已处理，继续下一条即可\n'
                f'- 已有序号: {existing_record.get("sequence")}\n'
                f'- 已有文件: {existing_record.get("file_name", "")}\n'
                f'- 图片 URL: {image_url}\n'
                f'- 页面 URL: {existing_record.get("page_url", "") or page_url or "未提供"}\n'
                f'{manifest_note}'
            )
            existing_file = IMAGE_DIR / Path(str(existing_record.get('file_name') or '')).name
            attachments = [str(existing_file)] if existing_file.exists() else []
            return ActionResult(
                extracted_content=msg,
                include_in_memory=True,
                long_term_memory=f'图片 URL 已记录，跳过重复下载: {existing_record.get("file_name", "")}',
                attachments=attachments,
            )

        file_prefix = _prefix_from_filename(params.file_name, 'temple')
        sequence, sequence_note = _safe_requested_image_sequence(params.sequence, params.record_filename, file_prefix)
        file_name = _numbered_file_stem(params.file_name, sequence, file_prefix)
        title = _renumber_title_if_needed(params.title, sequence)
        border_ratio = _normalize_border_ratio(params.border_ratio)

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
        existing_content_record = _find_downloaded_record_by_file_hash(params.record_filename, file_hash)
        if existing_content_record:
            image_path.unlink(missing_ok=True)
            existing_file = IMAGE_DIR / Path(str(existing_content_record.get('file_name') or '')).name
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

        existing_image_path = _find_existing_image_file_by_hash(file_hash, exclude_path=image_path)
        if existing_image_path:
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
            if image_path and image_path.exists():
                image_path.unlink(missing_ok=True)
            return record_result

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


def _existing_recorded_image_urls(record_filename: str) -> set[str]:
    record_file = AGENT_DATA_DIR / _safe_agent_data_filename(record_filename, 'image_record.jsonl')
    return {
        str(record.get('image_url') or '').strip()
        for record in _load_image_records(record_file)
        if record.get('status') == 'downloaded' and record.get('image_url')
    }


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


@tools.action(
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


@tools.action(
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


@tools.action(
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


@tools.action(
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


@tools.action(
    description='提取当前网页源代码中符合Information.md文件中HTML代码块首尾行的部分并保存为文件',
    param_model=ExtractPageContentParams,
)
async def extract_page_to_markdown(params: ExtractPageContentParams, browser_session):
    """
    使用JavaScript提取网页源代码中符合Information.md文件中HTML代码块首尾行的部分。

    参数说明:
    - output_filename: 输出文件名
    - output_dir: 输出目录
    - format_type: 格式类型，可选 'markdown'/'json'/'text'
    - information_file_path: Information.md文件路径
    """
    try:
        info_file_path, output_dir_path = _resolve_extract_paths(params)
        search_patterns = _load_information_patterns(info_file_path)

        # JavaScript：获取网页源代码并查找匹配的代码块
        js_code = f'''
        (function() {{
            try {{
                const fullHtml = document.documentElement.outerHTML;
                const searchPatterns = {json_module.dumps(search_patterns, ensure_ascii=False)};
                const foundBlocks = [];

                for (const pattern of searchPatterns) {{
                    const escapeRegExp = (string) => {{
                        return string.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\$&');
                    }};
                    const startPattern = escapeRegExp(pattern.start);
                    const endPattern = escapeRegExp(pattern.end);
                    const regex = new RegExp(startPattern + '[\\\\s\\\\S]*?' + endPattern, 'gi');

                    let match;
                    while ((match = regex.exec(fullHtml)) !== null) {{
                        const alreadyExists = foundBlocks.some(block => block.content === match[0]);
                        if (!alreadyExists) {{
                            foundBlocks.push({{
                                original_start: pattern.start,
                                original_end: pattern.end,
                                content: match[0],
                                position: match.index
                            }});
                        }}
                    }}
                }}

                return {{
                    success: true,
                    url: window.location.href,
                    title: document.title,
                    found_blocks: foundBlocks,
                    total_found: foundBlocks.length,
                    search_patterns: searchPatterns
                }};
            }} catch (error) {{
                return {{
                    success: false,
                    error: error.message,
                    stack: error.stack
                }};
            }}
        }})()
        '''

        # 执行 JavaScript
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
            error_msg = data.get('error', '未知错误') if data else '未获取到数据'
            return ActionResult(error=f'提取失败: {error_msg}')

        found_blocks = data.get('found_blocks', [])
        page_title = data.get('title', '')
        page_url = data.get('url', '')

        if not found_blocks:
            return ActionResult(error="在网页源代码中未找到匹配的HTML代码块")

        # 根据格式类型生成内容
        file_content, file_ext = _format_output(
            found_blocks, page_title, page_url, params.format_type
        )

        # 清理文件名中的非法字符
        safe_filename = _safe_extract_filename(params.output_filename, file_ext)

        # 构建完整路径（使用已验证的 output_dir）
        output_path = _write_extracted_file(output_dir_path, safe_filename, file_content)

        success_msg = (
            f"✅ 成功提取网页中匹配的HTML代码块并保存到: {output_path}\n"
            f"格式: {params.format_type.upper()}\n"
            f"共找到 {len(found_blocks)} 个匹配块"
        )

        return ActionResult(
            extracted_content=success_msg,
            include_in_memory=True,
            long_term_memory=f'已将当前网页中匹配Information.md的HTML代码块提取并保存到 {safe_filename} (格式: {params.format_type})',
        )

    except Exception as e:
        return ActionResult(error=f'提取网页内容时出错: {str(e)}')


def _format_output(
    found_blocks: list[dict],
    page_title: str,
    page_url: str,
    format_type: str,
) -> tuple[str, str]:
    """根据格式类型生成输出内容，返回 (content, file_extension)。"""
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

    # markdown（默认）
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


@tools.action(
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
