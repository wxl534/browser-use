"""
配置驱动的通用 IIIF 适配器.

无需为每个站点写 Python 代码:只要该站点是「搜索栏 + item 列表 + IIIF manifest」
结构,就把站点差异塞进一个 JSON profile,`ConfigIIIFAdapter` 直接读 profile 跑通
批量下载流水线.profile 可由 ``profile_site.py`` 自动读取目标站 DOM 生成,再人工
确认.

profile 字段(site_profiles/<site_id>.json)::

    {
      "site_id": "gallica",
      "host_suffixes": ["gallica.bnf.fr"],
      "search_url_template": "https://gallica.bnf.fr/services/Search?keyword={keyword}&page={page}",
      "results_host": "gallica.bnf.fr",
      "results_path": "/services/search",
      "keyword_param": "keyword",
      "page_param": "page",
      "limit_param": "limit",
      "item_link_selector": "a[href*='/ark:/']",
      "item_id_regex": "/ark:/([0-9a-z/]+)",
      "manifest_template": "https://gallica.bnf.fr/iiif/ark:/{id}/manifest.json"
    }
"""
from __future__ import annotations

import json as json_module
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from adapters.base import SearchPageResult
from adapters.iiif import IIIFAdapter


# 通用搜索结果页提取 JS:选择器 + id 正则 + manifest 模板全部参数化.
_GENERIC_EXTRACT_JS_TEMPLATE = r'''
(function() {
    try {
        const seen = new Set();
        const items = [];
        const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const selector = __SELECTOR__;
        const idRe = new RegExp(__ID_REGEX__);
        const manifestTpl = __MANIFEST_TPL__;
        for (const link of document.querySelectorAll(selector)) {
            const href = link.href || link.getAttribute('href') || '';
            let url;
            try { url = new URL(href, window.location.href).href; } catch (_) { continue; }
            const match = url.match(idRe);
            if (!match) continue;
            const id = match[1];
            if (seen.has(id)) continue;
            seen.add(id);
            const container = link.closest('article, li, .card, .result, .item, div') || link;
            const title = clean(link.getAttribute('title') || link.textContent || (container && container.textContent) || id);
            items.push({
                id,
                url,
                title,
                manifest_url: manifestTpl ? manifestTpl.replace('{id}', id) : '',
            });
        }
        return {
            success: true,
            page_url: window.location.href,
            page_title: document.title,
            total_found: items.length,
            body_text_length: (document.body && document.body.innerText || '').trim().length,
            anchor_count: document.querySelectorAll('a').length,
            collection_link_count: document.querySelectorAll(selector).length,
            items: items.slice(__START__, __END__),
        };
    } catch (error) {
        return {success: false, error: error.message, stack: error.stack};
    }
})()
'''


class ConfigIIIFAdapter(IIIFAdapter):
    """读取 JSON profile 跑批量下载的通用 IIIF 适配器,零站点专属代码."""

    def __init__(self, profile: dict):
        missing = [k for k in ('site_id', 'item_link_selector', 'item_id_regex', 'manifest_template') if not profile.get(k)]
        if missing:
            raise ValueError(f'profile 缺少必填字段: {", ".join(missing)}')
        self._p = dict(profile)
        self.site_id = str(profile['site_id']).strip().lower()

    def default_host_suffixes(self) -> list[str]:
        return [str(h).lower() for h in self._p.get('host_suffixes') or [] if h]

    def page_label(self) -> str:
        return str(self._p.get('site_label') or self.site_id.upper())

    def _kw_param(self) -> str:
        return str(self._p.get('keyword_param') or 'q')

    def _pg_param(self) -> str:
        return str(self._p.get('page_param') or 'page')

    def _lim_param(self) -> str:
        return str(self._p.get('limit_param') or 'limit')

    def build_search_url(self, keyword: str, page: int, limit: int) -> str:
        normalized = re.sub(r'\s+', ' ', str(keyword or '')).strip()
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            limit = min(100, max(1, int(limit)))
        except (TypeError, ValueError):
            limit = 50
        tpl = str(self._p.get('search_url_template') or '')
        if tpl:
            return tpl.format(keyword=normalized, page=page, limit=limit)
        base = str(self._p.get('search_url_base') or '')
        return base + '?' + urlencode({self._kw_param(): normalized, self._lim_param(): limit, self._pg_param(): page})

    def is_results_url(self, url: str) -> bool:
        if not url:
            return False
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or '').lower()
        path = (parsed.path or '').rstrip('/').lower()
        want_host = str(self._p.get('results_host') or '').lower()
        want_path = str(self._p.get('results_path') or '').rstrip('/').lower()
        query = parse_qs(parsed.query)
        host_ok = host == want_host if want_host else any(host.endswith(h) for h in self.default_host_suffixes())
        path_ok = path == want_path if want_path else True
        return host_ok and path_ok and self._kw_param() in query

    def parse_search_url(self, url: str) -> tuple[str, int, int]:
        query = parse_qs(urlparse(url or '').query)
        keyword = (query.get(self._kw_param()) or [''])[0] or ''
        try:
            page = max(1, int((query.get(self._pg_param()) or ['1'])[0]))
        except ValueError:
            page = 1
        try:
            limit = min(max(1, int((query.get(self._lim_param()) or ['50'])[0])), 100)
        except ValueError:
            limit = 50
        return keyword, page, limit

    async def extract_items(self, browser_session: Any, *, max_items: int, start_index: int) -> SearchPageResult:
        js_code = (
            _GENERIC_EXTRACT_JS_TEMPLATE
            .replace('__SELECTOR__', json_module.dumps(self._p['item_link_selector']))
            .replace('__ID_REGEX__', json_module.dumps(self._p['item_id_regex']))
            .replace('__MANIFEST_TPL__', json_module.dumps(self._p['manifest_template']))
            .replace('__START__', json_module.dumps(start_index))
            .replace('__END__', json_module.dumps(start_index + max_items))
        )
        cdp_session = await browser_session.get_or_create_cdp_session()
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
            session_id=cdp_session.session_id,
        )
        if result.get('exceptionDetails'):
            exc = result['exceptionDetails']
            raise RuntimeError((exc.get('exception') or {}).get('description') or exc.get('text') or '搜索结果提取失败')
        data = result.get('result', {}).get('value') or {}
        if not data.get('success'):
            raise RuntimeError(data.get('error', '搜索结果提取失败'))
        items = [i for i in data.get('items') or [] if isinstance(i, dict)]
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
