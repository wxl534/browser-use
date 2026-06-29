"""
IIIF Presentation API 适配器基类.

世界上很多文化遗产 / 图书馆 / 博物馆站点(IDP, LOC 一部分馆藏, BnF Gallica,
梵蒂冈图书馆, 大英图书馆, Bodleian, 京都大学 etc.)都暴露符合 IIIF
Presentation API 的 manifest JSON.一旦能拿到 manifest URL,提取真正可
下载的图片 URL 这件事就是**站点无关**的.

继承本类后只需要再给:
- ``site_id``
- ``build_search_url`` / ``is_results_url`` / ``parse_search_url``
- ``extract_items``
- ``manifest_url_for_item(item)``:从一个 search item 取出它对应的 manifest URL

就能把一个 IIIF 站点接入批量下载流水线.``resolve_item_image_urls`` 会
直接复用本类的 IIIF 解析逻辑.

注:解析在浏览器上下文里执行(``fetch()`` + ``Runtime.evaluate``).这
样可以共享当前会话的 cookies / referer,对个别要求登录或受限 referer 的
manifest 友好.绝大多数公共 IIIF manifest 也可以直接用 httpx 拉,对它们
后续可以再做一个 ``httpx`` 直拉版本以省一次 CDP roundtrip.
"""
from __future__ import annotations

import json as json_module
from abc import abstractmethod
from typing import Any

from adapters.base import ItemImageResolution, SearchPageResult, SiteAdapter


# IIIF manifest JSON 解析 JS:遍历 manifest 树,把所有 image service / image id
# 收集成可直接下载的 URL,并对常见噪音(thumbnail/logo/sprite)打分降权.
_IIIF_MANIFEST_FETCH_JS_TEMPLATE = r'''
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
        if (!/^https?:\/\//i.test(url)) return;
        if (/\/image\/iiif\/3\/[^/]+$/i.test(new URL(url).pathname)) {
            url = url.replace(/\/$/, '') + '/full/max/0/default.jpg';
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
        if (typeof id === 'string' && (/\/image\/iiif\//i.test(id) || /\/mediaLib\//i.test(id))) {
            addUrl(urls, seen, id);
        }
        const service = node.service || node.services;
        if (service) walk(service, urls, seen);
        for (const child of Object.values(node)) walk(child, urls, seen);
    };
    const score = (url) => {
        let value = 0;
        if (/\/image\/iiif\//i.test(url)) value += 500;
        if (/\/full\/(?:max|full)\//i.test(url)) value += 220;
        if (/default\.(?:jpe?g|png|webp|tiff?)(?:[?#]|$)/i.test(url)) value += 80;
        if (/\/mediaLib\//i.test(url)) value += 60;
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
})(__MANIFEST_URL_JSON__)
'''


def _build_manifest_fetch_js(manifest_url: str) -> str:
    return _IIIF_MANIFEST_FETCH_JS_TEMPLATE.replace(
        '__MANIFEST_URL_JSON__',
        json_module.dumps(manifest_url, ensure_ascii=False),
    )


async def fetch_iiif_manifest_in_browser(browser_session: Any, manifest_url: str) -> dict:
    """
    在当前浏览器上下文里 fetch + 解析一个 IIIF manifest.

    返回 dict 字段:``label`` / ``summary`` / ``metadata`` / ``image_urls``.
    解析失败时抛 ``RuntimeError``.
    """
    js_code = _build_manifest_fetch_js(manifest_url)
    cdp_session = await browser_session.get_or_create_cdp_session()
    result = await cdp_session.cdp_client.send.Runtime.evaluate(
        params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
        session_id=cdp_session.session_id,
    )
    if result.get('exceptionDetails'):
        raise RuntimeError(result['exceptionDetails'].get('text', 'IIIF manifest 解析失败'))
    data = result.get('result', {}).get('value') or {}
    if not data.get('success'):
        raise RuntimeError(data.get('error', 'IIIF manifest 解析失败'))
    return data


class IIIFAdapter(SiteAdapter):
    """
    任何 IIIF Presentation API 站点的共同基类.
    子类只需要再实现:URL 函数,``extract_items``,``manifest_url_for_item``.
    """

    @abstractmethod
    def manifest_url_for_item(self, item: dict) -> str:
        """从 search-result item 提取该 item 的 IIIF manifest URL."""

    async def resolve_item_image_urls(
        self,
        browser_session: Any,
        item: dict,
    ) -> ItemImageResolution:
        manifest_url = (self.manifest_url_for_item(item) or '').strip()
        if not manifest_url:
            raise RuntimeError('item 缺少 manifest URL,无法解析 IIIF 图片')
        manifest = await fetch_iiif_manifest_in_browser(browser_session, manifest_url)
        return ItemImageResolution(
            image_urls=[str(url) for url in manifest.get('image_urls') or [] if url],
            label=str(manifest.get('label') or item.get('title') or item.get('id') or '').strip(),
            metadata=str(manifest.get('metadata') or '').strip(),
            summary=str(manifest.get('summary') or '').strip(),
        )

    # ``extract_items`` 仍由各子站点自行实现,因为不同站点搜索结果页 DOM 差异巨大.
    @abstractmethod
    async def extract_items(
        self,
        browser_session: Any,
        *,
        max_items: int,
        start_index: int,
    ) -> SearchPageResult: ...


# ----------------------------------------------------------------------
# 新接入一个 IIIF 站点的模板(伪代码,未注册到运行时):
#
# class GallicaAdapter(IIIFAdapter):
#     site_id = 'gallica'
#
#     def build_search_url(self, keyword, page, limit):
#         return f'https://gallica.bnf.fr/services/Search?keyword={keyword}&page={page}'
#
#     def is_results_url(self, url):
#         return 'gallica.bnf.fr/services/Search' in url
#
#     def parse_search_url(self, url):
#         # parse_qs(...) -> (keyword, page, limit)
#         ...
#
#     async def extract_items(self, browser_session, *, max_items, start_index):
#         # 跑一段 JS 扫 Gallica 搜索结果 DOM,返回 SearchPageResult
#         ...
#
#     def manifest_url_for_item(self, item):
#         return f'https://gallica.bnf.fr/iiif/ark:/{item["ark"]}/manifest.json'
# ----------------------------------------------------------------------
