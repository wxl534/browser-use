"""idp.bl.uk(British Library International Dunhuang Project)站点插件.

把 IDP 特有的 URL 判定 / IIIF manifest 推导 / item id 提取 / 抓取进度 helper /
参数模型集中在此,并通过 register_download_site_hint 注入通用下载/发号工具.
tools_registry 通用核心因此不含任何 idp.bl.uk 硬编码.

注意:本模块从 tools_registry 导入共享基建,因此 tools_registry 必须在其底部
(所有共享名定义完成后)再 `import sites.idp`,避免循环导入.
"""
import json as json_module
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from tools_registry import (
    AGENT_DATA_DIR,
    _clean_url_text,
    register_download_site_hint,
)


# === IDP 特有的 URL 判定 / 推导 ===


def idp_is_invalid_collection_url(url: str) -> bool:
    """idp.bl.uk 上「collection/<非 24-64 位 hex>」属于搜索/列表页,不是合法详情页."""
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


def idp_is_valid_page_url(url: str) -> bool:
    """idp.bl.uk 上 collection 根页或 collection/<24-64 位 hex> 详情页视为合法."""
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


def idp_manifest_url_from_page_url(page_url: str) -> str:
    """从 IDP 详情页 URL 推导 data.idp.bl.uk 的 IIIF v3 manifest URL."""
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


def idp_item_id_from_urls(page_url: str, image_url: str = '') -> str:
    """从 IDP 的详情页 / manifest / iiif URL 提取站内 item id(大写)."""
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


# === IDP 抓取进度(navigate_idp_search_page 工具与 main.py 续跑使用)===


def _idp_progress_file() -> Path:
    # 用实时 AGENT_DATA_DIR(configure_runtime_paths 之后会更新),
    # 否则会落到导入时捕获的 repo 默认目录,与 worker 写入的 run_dir/idp_progress.json 脱节。
    import tools_registry as _tr
    return _tr.AGENT_DATA_DIR / 'idp_progress.json'


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


# === IDP 工具参数模型 ===


class NavigateIdpSearchPageParams(BaseModel):
    """跳转 IDP 搜索结果页的参数模型"""
    keyword: str = Field(default='china temple', description='搜索关键词,默认 china temple')
    page: str | int = Field(default=1, description='页码;工具会从类似 2D 的脏值中提取数字')
    limit: str | int = Field(default=50, description='每页条数;工具会限制到 1-100')


class DownloadCurrentIdpSearchPageImagesParams(BaseModel):
    """批量下载当前 IDP 搜索结果页中的图片."""
    target_count: int = Field(default=1000, ge=1, description='总目标有效记录数,达到后自动停止')
    max_items: int = Field(default=50, ge=1, le=100, description='当前搜索页最多处理多少个藏品结果')
    start_index: int = Field(default=0, ge=0, description='从当前搜索页第几个结果开始处理,0 表示第一个')
    images_per_item: int = Field(default=1, ge=1, le=5, description='每个藏品最多保存几张图片,默认只保存主图')
    file_prefix: str = Field(default='temple', description='保存文件名前缀,例如 temple')
    title_prefix: str = Field(default='china_temple', description='图片标题/文件名前缀')
    allowed_host_suffixes: list[str] = Field(
        default_factory=lambda: ['idp.bl.uk', 'data.idp.bl.uk', 'bl.uk'],
        description='允许下载的官方域名后缀',
    )
    record_filename: str = Field(default='image_record.jsonl', description='结构化记录文件名')
    info_filename: str = Field(default='temple_photo_info.md', description='Markdown 信息表文件名')
    timeout_seconds: int = Field(default=120, ge=30, le=900, description='Python 直接下载单张图片的超时时间')


# === 把 IDP 能力注入通用下载/发号工具 ===
register_download_site_hint(
    ['idp.bl.uk', 'data.idp.bl.uk'],
    manifest_from_page_url=idp_manifest_url_from_page_url,
    is_invalid_collection_url=idp_is_invalid_collection_url,
    is_valid_page_url=idp_is_valid_page_url,
    item_id_from_urls=idp_item_id_from_urls,
    item_link_selector='a[href*="/collection/"]',
)
