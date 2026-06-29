"""
站点适配器接口.

``SiteAdapter`` 描述了"批量下载编排器需要从一个目标网站知道什么",
只关心**站点差异**:URL 模板,搜索结果页 DOM,item 到图片 URL 的解析方式.

页面状态机,并发拉取,去重,JSONL 落库,进度文件这些**通用**逻辑都不在
适配器里,由 :mod:`core.batch_download` 负责.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchPageResult:
    """搜索结果页一次提取的产物."""
    items: list[dict] = field(default_factory=list)
    page_url: str = ''
    total_found: int = 0
    body_text_length: int = 0
    anchor_count: int = 0
    collection_link_count: int = 0
    debug: dict = field(default_factory=dict)


@dataclass
class ItemImageResolution:
    """单个 item 的图片解析结果."""
    image_urls: list[str] = field(default_factory=list)
    label: str = ''
    metadata: str = ''
    summary: str = ''


class SiteAdapter(ABC):
    """
    站点适配器基类.子类只需关注站点差异;公共逻辑全部在编排器里.

    ``site_id`` 用于命名所有 per-site 状态文件和环境变量前缀,必须是
    短小,ascii,小写的标识符,例如 ``'idp'`` / ``'loc'`` / ``'gallica'``.
    """

    site_id: str = ''

    # ---------- 文件 / 环境变量命名约定(可被子类覆盖) ----------

    @property
    def progress_file_name(self) -> str:
        return f'{self.site_id}_progress.json'

    @property
    def page_progress_file_name(self) -> str:
        return f'{self.site_id}_page_progress.json'

    @property
    def empty_page_events_file_name(self) -> str:
        return f'{self.site_id}_empty_page_events.jsonl'

    @property
    def consecutive_failure_env_var(self) -> str:
        return f'BROWSER_USE_{self.site_id.upper()}_MAX_CONSECUTIVE_BATCH_FAILURES'

    @property
    def batch_item_cap_env_var(self) -> str:
        return f'BROWSER_USE_{self.site_id.upper()}_BATCH_ITEM_CAP'

    def consecutive_failure_threshold(self) -> int | None:
        raw = os.environ.get(self.consecutive_failure_env_var, '').strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def batch_item_cap(self, requested_max_items: int) -> int:
        raw = os.environ.get(self.batch_item_cap_env_var, '').strip()
        if not raw:
            return requested_max_items
        try:
            return min(100, max(1, int(raw)))
        except ValueError:
            return requested_max_items

    # ---------- URL 模板 / 解析 ----------

    @abstractmethod
    def build_search_url(self, keyword: str, page: int, limit: int) -> str:
        """根据关键词 + 页码 + 每页条数构造该站点的搜索 URL(canonical 形式)."""

    @abstractmethod
    def is_results_url(self, url: str) -> bool:
        """判断给定 URL 是不是该站点的搜索结果页."""

    @abstractmethod
    def parse_search_url(self, url: str) -> tuple[str, int, int]:
        """解析搜索 URL,返回 (keyword, page, limit)."""

    def canonical_resume_url(self, progress: dict) -> str | None:
        """
        当 agent 漂移到非搜索页时,根据 progress 文件给出应当跳回的 URL.
        默认实现:用 progress 中的 keyword / next_page / limit + ``build_search_url``.
        """
        keyword = str(progress.get('keyword') or '').strip()
        if not keyword:
            return None
        try:
            page = int(progress.get('next_page') or progress.get('current_page') or 1)
        except (TypeError, ValueError):
            page = 1
        try:
            limit = int(progress.get('limit') or 50)
        except (TypeError, ValueError):
            limit = 50
        return self.build_search_url(keyword, page, limit)

    # ---------- 站点行为 ----------

    @abstractmethod
    async def extract_items(
        self,
        browser_session: Any,
        *,
        max_items: int,
        start_index: int,
    ) -> SearchPageResult:
        """在当前浏览器 tab 上扫搜索结果,返回切片后的 items."""

    @abstractmethod
    async def resolve_item_image_urls(
        self,
        browser_session: Any,
        item: dict,
    ) -> ItemImageResolution:
        """根据单个 item 解析出可直接下载的图片 URL(以及 label/metadata/summary)."""

    # ---------- 杂项 ----------

    def default_host_suffixes(self) -> list[str]:
        """该站点允许的图片 host 后缀;返回空列表表示由上层 sanitizer 决定."""
        return []

    def evidence_template(self, keyword: str) -> str:
        """落 image_record.jsonl 时写的 evidence 字段模板."""
        return f'{self.site_id.upper()} search result for {keyword or self.site_id.upper()}.'

    def page_label(self) -> str:
        """日志里友好的站点显示名."""
        return self.site_id.upper()
