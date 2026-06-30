"""
International Dunhuang Programme (idp.bl.uk) 适配器.

把原先散落在 ``tools_registry.py`` 里的 IDP 特定 URL / DOM / IIIF manifest
逻辑收拢到本文件.``core.batch_download`` 调度器在运行时只跟
``IDPAdapter`` 的方法打交道,不再硬编码 IDP.
"""
from __future__ import annotations

import json as json_module
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from adapters.base import SearchPageResult
from adapters.detail_overview import fetch_detail_overview_in_browser
from adapters.iiif import IIIFAdapter


# IDP 详情页(idp.bl.uk/collection/{ID}/)是服务端渲染 HTML,Overview 字段以
# label-value 形式排在 DOM 里——这正是通用详情引擎的 ``sections`` 模式.IDP 因此
# 只是该引擎的一个「profile 实例」:页眉标题/材质 + 每个字段一个
# .detaildropdown__section(<h4>标签</h4> + 值/facet 链接).引擎在浏览器上下文
# 同源 fetch 该页(自动带 cf_clearance cookie 过 CF),DOMParser 确定性解析,绝不编造.
_IDP_DETAIL_OVERVIEW_CONFIG = {
    'mode': 'sections',
    'section_selector': '.detaildropdown__section',
    'label_selector': 'h4',
    'header_fields': [
        {'label': 'Pressmark', 'selector': '.collectionheader__pressmark h1'},
        {'label': 'Material', 'selector': '.collectionheader__material h2'},
    ],
}


def _idp_detail_url_for_item(item: dict) -> str:
    """从 search item 推出详情页 URL(优先用 item['url'],兜底用 id 拼)."""
    url = str(item.get('url') or '').strip()
    if url:
        return url
    item_id = str(item.get('id') or '').strip().upper()
    if re.fullmatch(r'[A-F0-9]{24,64}', item_id):
        return f'https://idp.bl.uk/collection/{item_id}/'
    return ''


_IDP_SEARCH_EXTRACT_JS_TEMPLATE = r'''
(function() {
    try {
        const seen = new Set();
        const items = [];
        const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        for (const link of document.querySelectorAll('a[href*="/collection/"]')) {
            const href = link.href || link.getAttribute('href') || '';
            let url;
            try {
                url = new URL(href, window.location.href).href;
            } catch (_) {
                continue;
            }
            const match = url.match(/\/collection\/([A-Fa-f0-9]{24,64})\/?/);
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
            items: items.slice(__START__, __END__),
        };
    } catch (error) {
        return {success: false, error: error.message, stack: error.stack};
    }
})()
'''


def _build_search_extract_js(start_index: int, max_items: int) -> str:
    return (
        _IDP_SEARCH_EXTRACT_JS_TEMPLATE
        .replace('__START__', json_module.dumps(start_index))
        .replace('__END__', json_module.dumps(start_index + max_items))
    )


class IDPAdapter(IIIFAdapter):
    """idp.bl.uk 的 SiteAdapter 实现."""

    site_id = 'idp'

    # 兼容旧文件名:idp_progress.json / idp_page_progress.json / idp_empty_page_events.jsonl
    # 由基类按 site_id 自动生成.

    def default_host_suffixes(self) -> list[str]:
        return ['idp.bl.uk', 'data.idp.bl.uk', 'iiif.io']

    def evidence_template(self, keyword: str) -> str:
        return (
            f'IDP search result for {keyword or "IDP"}; '
            'image URL resolved from official IIIF manifest.'
        )

    # ---------- URL ----------

    def build_search_url(self, keyword: str, page: int, limit: int) -> str:
        normalized_keyword = re.sub(r'\s+', ' ', str(keyword or '')).strip() or 'china temple'
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            limit = min(100, max(1, int(limit)))
        except (TypeError, ValueError):
            limit = 50
        return 'https://idp.bl.uk/collection/?' + urlencode({
            'term': normalized_keyword,
            'limit': limit,
            'page': page,
        })

    def is_results_url(self, url: str) -> bool:
        if not url:
            return False
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or '').lower()
        path = (parsed.path or '').rstrip('/').lower()
        query = parse_qs(parsed.query)
        return host == 'idp.bl.uk' and path in {'/collection', ''} and 'term' in query

    def parse_search_url(self, url: str) -> tuple[str, int, int]:
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
        return keyword, page, limit

    # ---------- 行为 ----------

    async def extract_items(
        self,
        browser_session: Any,
        *,
        max_items: int,
        start_index: int,
    ) -> SearchPageResult:
        js_code = _build_search_extract_js(start_index, max_items)
        cdp_session = await browser_session.get_or_create_cdp_session()
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
            session_id=cdp_session.session_id,
        )
        if result.get('exceptionDetails'):
            exc_details = result['exceptionDetails']
            detail = (
                (exc_details.get('exception') or {}).get('description')
                or exc_details.get('text')
                or 'IDP 搜索结果提取失败'
            )
            raise RuntimeError(detail)
        data = result.get('result', {}).get('value') or {}
        if not data.get('success'):
            raise RuntimeError(data.get('error', 'IDP 搜索结果提取失败'))
        items = [item for item in data.get('items') or [] if isinstance(item, dict)]
        return SearchPageResult(
            items=items,
            page_url=str(data.get('page_url') or ''),
            total_found=int(data.get('total_found') or 0),
            body_text_length=int(data.get('body_text_length') or 0),
            anchor_count=int(data.get('anchor_count') or 0),
            collection_link_count=int(data.get('collection_link_count') or 0),
            debug={'page_title': data.get('page_title') or ''},
        )

    def manifest_url_for_item(self, item: dict) -> str:
        return str(item.get('manifest_url') or '')

    async def resolve_item_detail_overview(
        self,
        browser_session: Any,
        item: dict,
    ) -> dict:
        """
        抓取 IDP 详情页(idp.bl.uk/collection/{ID}/)的结构化 Overview 元数据.

        IDP 的 IIIF manifest 只含 Pressmark/Description/Reading Direction 3 个
        字段;Date/Find site/Measurement/Language/Subject/Institution/Provenance
        等只在详情页 HTML 里.IDP 是通用详情引擎 ``sections`` 模式的一个 profile
        实例(见 ``_IDP_DETAIL_OVERVIEW_CONFIG``),引擎在浏览器同源 fetch 该页
        (自动带 cf_clearance cookie 过 CF),DOMParser 确定性解析,绝不编造.
        失败优雅返回空 dict,不影响图片下载.
        """
        return await fetch_detail_overview_in_browser(
            browser_session,
            _idp_detail_url_for_item(item),
            _IDP_DETAIL_OVERVIEW_CONFIG,
        )
