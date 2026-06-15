"""
站点适配器（SiteAdapter）集合。

每个适配器把"如何在某个目标网站上完成一次搜索 -> 取 item -> 解析图片 URL"
的差异封装成一个类，给 ``core.batch_download.run_search_page_batch`` 调度器使用。

新加一个长得像 IDP 的站点时，只需在本目录新增一个 ``*.py``：
- 继承 :class:`adapters.iiif.IIIFAdapter`（如果该站点提供 IIIF Presentation API）
  或 :class:`adapters.base.SiteAdapter`
- 实现 ``site_id`` / URL 构造 / 搜索结果 DOM 提取
"""
from adapters.base import SearchPageResult, ItemImageResolution, SiteAdapter
from adapters.iiif import IIIFAdapter
from adapters.idp import IDPAdapter

__all__ = [
    'SearchPageResult',
    'ItemImageResolution',
    'SiteAdapter',
    'IIIFAdapter',
    'IDPAdapter',
]
