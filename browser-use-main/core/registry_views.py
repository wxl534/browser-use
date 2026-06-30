"""工具参数模型与共享常量(从 tools_registry.py 拆出的零副作用层).

这里只放**纯数据**:Pydantic 参数模型 + 不依赖运行时状态的常量.
任何带副作用/依赖运行时路径或 record helper 的逻辑都不放这里,
以保证本模块可被任意子模块安全导入,不产生 import 循环.

注意:本模块与 tools_registry.py 同在 core/ 目录,故 ``Path(__file__)``
推导出的 image / legacy 默认路径与原先一致.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

# 图片扩展名 → PIL 保存格式
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


# === 工具参数模型 ===

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
