"""
International Dunhuang Programme (idp.bl.uk) 适配器。

把原先散落在 ``tools_registry.py`` 里的 IDP 特定 URL / DOM / IIIF manifest
逻辑收拢到本文件。``core.batch_download`` 调度器在运行时只跟
``IDPAdapter`` 的方法打交道，不再硬编码 IDP。
"""
from __future__ import annotations

import json as json_module
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from adapters.base import SearchPageResult
from adapters.iiif import IIIFAdapter


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
    """idp.bl.uk 的 SiteAdapter 实现。"""

    site_id = 'idp'

    # 兼容旧文件名：idp_progress.json / idp_page_progress.json / idp_empty_page_events.jsonl
    # 由基类按 site_id 自动生成。

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
            raise RuntimeError(result['exceptionDetails'].get('text', 'IDP 搜索结果提取失败'))
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
