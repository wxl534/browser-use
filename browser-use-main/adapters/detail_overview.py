"""
通用「详情页 Overview 元数据」提取引擎(配置驱动,零 LLM,绝不编造).

很多图库 / IIIF 站点的 item 详情页是服务端渲染 HTML,把结构化元数据
(标题 / 年代 / 尺寸 / 语言 / 出处 ...)以 label-value 形式排在页面里.常见
DOM 模式只有三种:

- ``sections`` : 每个字段一个容器(``section_selector``),容器里有标签元素
  (``label_selector``,如 ``h4``)和值(``value_selector`` 或容器去掉标签后的
  文本 / facet 链接).IDP 的 ``.detaildropdown__section`` 属于此类.
- ``dl``       : ``<dl><dt>标签</dt><dd>值</dd></dl>`` 定义列表.
- ``table``    : ``<table><tr><th>标签</th><td>值</td></tr></table>`` 表格.

只要在站点 profile 里声明用哪种模式 + 选择器,本引擎就能对任意同结构站点
通用抽取,无需为每个站点写 JS.

config 字段::

    {
      "mode": "sections",              # sections | dl | table | manifest_only
      "section_selector": ".detaildropdown__section",  # sections/dl/table 的容器
      "label_selector": "h4",          # 仅 sections 模式:标签元素
      "value_selector": "p",           # 仅 sections 模式(可选):值元素;省略=容器去标签文本
      "header_fields": [               # 可选:不在上述模式里的零散字段(如页眉标题)
        {"label": "Pressmark", "selector": ".collectionheader__pressmark h1"}
      ]
    }

``mode == "manifest_only"`` 或 config 为空时,引擎不工作(由 manifest 元数据负责).
"""
from __future__ import annotations

import json as json_module
from typing import Any

from adapters.iiif import evaluate_js_in_browser


# 通用详情解析 JS:在浏览器上下文同源 fetch 详情页 HTML(自动带 cookie 过 CF),
# 用 DOMParser 按 config 指定的模式确定性解析 label-value.绝不编造,失败返回
# success:false.
_DETAIL_OVERVIEW_JS_TEMPLATE = r'''
(async function(detailUrl, cfg) {
    const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
    try {
        const resp = await fetch(detailUrl, {credentials: 'include', headers: {'Accept': 'text/html'}});
        if (!resp.ok) {
            return {success: false, error: `HTTP ${resp.status}: ${resp.statusText}`, detail_url: detailUrl};
        }
        const html = await resp.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const overview = {};
        const order = [];
        const put = (k, v) => {
            k = clean(k); v = clean(v);
            if (k && v && !(k in overview)) { overview[k] = v; order.push(k); }
        };
        const joinUnique = (arr) => Array.from(new Set(arr.filter(Boolean))).join(', ');
        const valueOf = (el, valueSelector) => {
            if (valueSelector) {
                const vs = Array.from(el.querySelectorAll(valueSelector)).map((x) => clean(x.textContent));
                const joined = joinUnique(vs);
                if (joined) return joined;
            }
            const links = Array.from(el.querySelectorAll('a')).map((a) => clean(a.textContent));
            const joinedLinks = joinUnique(links);
            if (joinedLinks) return joinedLinks;
            return '';
        };

        // honor explicitly configured header_fields first
        for (const hf of (cfg.header_fields || [])) {
            if (!hf || !hf.selector || !hf.label) continue;
            const el = doc.querySelector(hf.selector);
            if (el) put(hf.label, el.textContent);
        }

        // AUTO-DETECT common header/title/pressmark if not provided
        try {
            // common selectors seen on museum/gallery sites
            const press = doc.querySelector('.collectionheader__pressmark h1, .collectionheader h1, .pressmark, .collection-pressmark, .collectionheader__pressmark');
            if (press) put('Pressmark', clean(press.textContent));

            const titleEl = doc.querySelector('h1, h2');
            if (titleEl) put('Title', clean(titleEl.textContent));

            // try to detect short identifier like F1982.2 (heuristic)
            const potential = Array.from(doc.querySelectorAll('h1, h2, h3, .pressmark, .collectionheader__pressmark, .collectionheader')).map(e=>clean(e.textContent));
            const idRe = /\b[A-Z][-A-Z0-9]{2,}\.?\d+\b/; // loose heuristic
            for (const t of potential) {
                const m = (t || '').match(idRe);
                if (m) { put('Identifier', m[0]); break; }
            }
        } catch (e) {
            // non-fatal
        }

        const mode = cfg.mode || 'sections';
        if (mode === 'dl') {
            const container = cfg.section_selector || 'dl';
            for (const dl of doc.querySelectorAll(container)) {
                const dts = dl.querySelectorAll('dt');
                const dds = dl.querySelectorAll('dd');
                const n = Math.min(dts.length, dds.length);
                for (let i = 0; i < n; i++) put(dts[i].textContent, dds[i].textContent);
            }
        } else if (mode === 'table') {
            const container = cfg.section_selector || 'table';
            for (const tr of doc.querySelectorAll(container + ' tr')) {
                const th = tr.querySelector('th');
                const td = tr.querySelector('td');
                if (th && td) put(th.textContent, td.textContent);
            }
        } else {
            // flexible default selectors: many detail pages use .detaildropdown__section or similar
            const sectionSel = cfg.section_selector || '.detaildropdown__section, .detail-section, .detail-section__item, .metadata__item, .detaildropdown__row, dl';
            const labelSel = cfg.label_selector || 'h4';

            // try to parse sections using the section selector(s)
            for (const sel of sectionSel.split(',')) {
                const s = sel.trim();
                if (!s) continue;
                for (const sec of doc.querySelectorAll(s)) {
                    const lbl = sec.querySelector(labelSel) || sec.querySelector('strong') || sec.querySelector('b');
                    if (!lbl) continue;
                    const label = clean(lbl.textContent);
                    if (!label) continue;
                    let value = valueOf(sec, cfg.value_selector);
                    if (!value) {
                        const clone = sec.cloneNode(true);
                        const lc = clone.querySelector(labelSel) || clone.querySelector('strong') || clone.querySelector('b');
                        if (lc) lc.remove();
                        value = clean(clone.textContent);
                    }
                    put(label, value);
                }
            }

            // As a fallback: search for dl > dt/dd pairs anywhere
            try {
                for (const dl of doc.querySelectorAll('dl')) {
                    const dts = dl.querySelectorAll('dt');
                    const dds = dl.querySelectorAll('dd');
                    const n = Math.min(dts.length, dds.length);
                    for (let i = 0; i < n; i++) put(dts[i].textContent, dds[i].textContent);
                }
            } catch (e) { /* ignore */ }
        }
        return {success: true, detail_url: detailUrl, overview, order, field_count: order.length};
    } catch (error) {
        return {success: false, error: String((error && error.message) || error), detail_url: detailUrl};
    }
})(__DETAIL_URL_JSON__, __CONFIG_JSON__)
'''


def build_detail_overview_js(detail_url: str, config: dict) -> str:
    """把详情 URL + config 注入通用解析 JS 模板."""
    return (
        _DETAIL_OVERVIEW_JS_TEMPLATE
        .replace('__DETAIL_URL_JSON__', json_module.dumps(detail_url, ensure_ascii=False))
        .replace('__CONFIG_JSON__', json_module.dumps(config or {}, ensure_ascii=False))
    )


def overview_config_is_active(config: dict | None) -> bool:
    """判断该 config 是否需要抓详情页(非空且 mode 不是 manifest_only)."""
    if not config or not isinstance(config, dict):
        return False
    if str(config.get('mode') or 'sections').strip().lower() == 'manifest_only':
        return False
    # sections 必须有 section_selector;dl/table 可用默认容器;header_fields 也算有效
    if config.get('header_fields'):
        return True
    mode = str(config.get('mode') or 'sections').strip().lower()
    if mode in ('dl', 'table'):
        return True
    return bool(config.get('section_selector'))


async def fetch_detail_overview_in_browser(
    browser_session: Any,
    detail_url: str,
    config: dict,
) -> dict:
    """
    在浏览器上下文 fetch 详情页并按 config 解析,返回 ``{标签: 值}`` 字典.

    使用并发信号量和本地缓存以减少对 CDP 的阻塞与重复请求。任何失败都优雅返回空 dict。
    """
    detail_url = str(detail_url or '').strip()
    if not detail_url or not overview_config_is_active(config):
        return {}

    # simple cache key
    cache_key = detail_url + '|' + json_module.dumps(config or {}, ensure_ascii=False)
    now = time.time()
    cached = _OVERVIEW_HTTP_CACHE.get(cache_key)
    if cached and now - cached[0] < _DETAIL_OVERVIEW_CACHE_TTL:
        return cached[1]

    # browser-side concurrency semaphore + evaluate timeout
    # configurable via DETAIL_OVERVIEW_BROWSER_CONCURRENCY and DETAIL_OVERVIEW_CACHE_TTL
    try:
        browser_conc = int(os.environ.get('DETAIL_OVERVIEW_BROWSER_CONCURRENCY', '4'))
    except Exception:
        browser_conc = 4

    # lazily create semaphore in module-level variable if not exists
    global _DETAIL_OVERVIEW_BROWSER_SEMAPHORE, _OVERVIEW_BROWSER_CACHE
    try:
        _DETAIL_OVERVIEW_BROWSER_SEMAPHORE
    except NameError:
        _DETAIL_OVERVIEW_BROWSER_SEMAPHORE = asyncio.Semaphore(browser_conc)
    try:
        _OVERVIEW_BROWSER_CACHE
    except NameError:
        _OVERVIEW_BROWSER_CACHE = {}

    # check browser-side cache first
    bcache = _OVERVIEW_BROWSER_CACHE.get(cache_key)
    if bcache and now - bcache[0] < _DETAIL_OVERVIEW_CACHE_TTL:
        return bcache[1]

    async with _DETAIL_OVERVIEW_BROWSER_SEMAPHORE:
        # double-check caches after acquiring semaphore
        now = time.time()
        cached = _OVERVIEW_HTTP_CACHE.get(cache_key)
        if cached and now - cached[0] < _DETAIL_OVERVIEW_CACHE_TTL:
            return cached[1]
        bcache = _OVERVIEW_BROWSER_CACHE.get(cache_key)
        if bcache and now - bcache[0] < _DETAIL_OVERVIEW_CACHE_TTL:
            return bcache[1]

        try:
            # limit evaluate_js_in_browser time to avoid long CDP stalls
            start = time.time()
            data = await asyncio.wait_for(
                evaluate_js_in_browser(browser_session, build_detail_overview_js(detail_url, config)),
                timeout=30.0,
            )
            elapsed = time.time() - start
            _OVERVIEW_METRICS['total_eval_time_sec'] += elapsed
        except Exception:
            _OVERVIEW_METRICS['browser_failures'] += 1
            return {}

        if not isinstance(data, dict) or not data.get('success'):
            _OVERVIEW_METRICS['browser_failures'] += 1
            return {}
        overview = data.get('overview') or {}
        if not isinstance(overview, dict):
            _OVERVIEW_METRICS['browser_failures'] += 1
            return {}

        result = {str(k): str(v) for k, v in overview.items() if str(k).strip() and str(v).strip()}
        # cache both browser-cache and http-cache (single cache for simplicity)
        ts = time.time()
        _OVERVIEW_BROWSER_CACHE[cache_key] = (ts, result)
        _OVERVIEW_HTTP_CACHE[cache_key] = (ts, result)
        _OVERVIEW_METRICS['browser_success'] += 1
        return result


# Fallback: server-side HTML parse using httpx + BeautifulSoup
# Non-blocking async version with simple in-memory cache and concurrency limit
import asyncio
import os
import time

# concurrency and cache TTL configurable via env
_DETAIL_OVERVIEW_HTTP_CONCURRENCY = int(os.environ.get('DETAIL_OVERVIEW_HTTP_CONCURRENCY', '6'))
_DETAIL_OVERVIEW_CACHE_TTL = int(os.environ.get('DETAIL_OVERVIEW_CACHE_TTL', '600'))
_DETAIL_OVERVIEW_HTTP_SEMAPHORE = asyncio.Semaphore(_DETAIL_OVERVIEW_HTTP_CONCURRENCY)
_OVERVIEW_HTTP_CACHE: dict[str, tuple[float, dict]] = {}

# Lightweight metrics for monitoring throughput and cache behavior
_OVERVIEW_METRICS = {
    'total_requests': 0,
    'cache_hits': 0,
    'cache_misses': 0,
    'http_success': 0,
    'http_failures': 0,
    'browser_success': 0,
    'browser_failures': 0,
    'total_eval_time_sec': 0.0,
}


def get_overview_metrics(reset: bool = False) -> dict:
    """Return current metrics; if reset True, zero counters after read."""
    data = dict(_OVERVIEW_METRICS)
    if reset:
        for k in _OVERVIEW_METRICS:
            _OVERVIEW_METRICS[k] = 0 if isinstance(_OVERVIEW_METRICS[k], int) else 0.0
    return data


async def fetch_detail_overview_via_http(detail_url: str, config: dict) -> dict:
    """Async fallback parser using httpx.AsyncClient + BeautifulSoup.

    Uses a semaphore to limit concurrent HTTP fetches and a simple in-memory cache
    (TTL controlled by DETAIL_OVERVIEW_CACHE_TTL) to avoid re-fetching the same
    detail pages in a short window.
    """
    try:
        import httpx
        from bs4 import BeautifulSoup
    except Exception:
        return {}

    detail_url = str(detail_url or '').strip()
    if not detail_url or not overview_config_is_active(config):
        return {}

    cache_key = detail_url + '|' + json_module.dumps(config or {}, ensure_ascii=False)
    now = time.time()
    _OVERVIEW_METRICS['total_requests'] += 1
    cached = _OVERVIEW_HTTP_CACHE.get(cache_key)
    if cached and now - cached[0] < _DETAIL_OVERVIEW_CACHE_TTL:
        _OVERVIEW_METRICS['cache_hits'] += 1
        return cached[1]
    _OVERVIEW_METRICS['cache_misses'] += 1

    async with _DETAIL_OVERVIEW_HTTP_SEMAPHORE:
        # double-check cache after acquiring semaphore
        cached = _OVERVIEW_HTTP_CACHE.get(cache_key)
        if cached and now - cached[0] < _DETAIL_OVERVIEW_CACHE_TTL:
            _OVERVIEW_METRICS['cache_hits'] += 1
            return cached[1]

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(detail_url)
            if resp.status_code != 200:
                _OVERVIEW_METRICS['http_failures'] += 1
                return {}
            _OVERVIEW_METRICS['http_success'] += 1
            text = resp.text
            soup = BeautifulSoup(text, 'html.parser')

            def clean(s):
                return ' '.join(str(s or '').split()).strip()

            overview: dict = {}
            order: list[str] = []

            def put(k, v):
                k2 = clean(k)
                v2 = clean(v)
                if k2 and v2 and k2 not in overview:
                    overview[k2] = v2
                    order.append(k2)

            def value_of(el, value_selector=None):
                if value_selector:
                    vs = [clean(x.get_text()) for x in el.select(value_selector)]
                    joined = ', '.join(dict.fromkeys([v for v in vs if v]))
                    if joined:
                        return joined
                links = [clean(a.get_text()) for a in el.find_all('a')]
                joined_links = ', '.join(dict.fromkeys([l for l in links if l]))
                if joined_links:
                    return joined_links
                return ''

            # header fields
            for hf in (config.get('header_fields') or []):
                if not hf or not hf.get('selector') or not hf.get('label'):
                    continue
                el = soup.select_one(hf['selector'])
                if el:
                    put(hf['label'], el.get_text())

            # heuristics
            try:
                press = soup.select_one('.collectionheader__pressmark h1, .collectionheader h1, .pressmark, .collection-pressmark, .collectionheader__pressmark')
                if press:
                    put('Pressmark', press.get_text())
                title = soup.select_one('h1, h2')
                if title:
                    put('Title', title.get_text())
                potential = [clean(x.get_text()) for x in soup.select('h1, h2, h3, .pressmark, .collectionheader__pressmark, .collectionheader')]
                import re
                id_re = re.compile(r'\b[A-Z][-A-Z0-9]{2,}\.??\d+\b')
                for t in potential:
                    if not t:
                        continue
                    m = id_re.search(t)
                    if m:
                        put('Identifier', m.group(0))
                        break
            except Exception:
                pass

            mode = str(config.get('mode') or 'sections').strip().lower()
            if mode == 'dl':
                container = config.get('section_selector') or 'dl'
                for dl in soup.select(container):
                    dts = dl.find_all('dt')
                    dds = dl.find_all('dd')
                    n = min(len(dts), len(dds))
                    for i in range(n):
                        put(dts[i].get_text(), dds[i].get_text())
            elif mode == 'table':
                container = config.get('section_selector') or 'table'
                for tr in soup.select(container + ' tr'):
                    th = tr.find('th')
                    td = tr.find('td')
                    if th and td:
                        put(th.get_text(), td.get_text())
            else:
                section_sel = config.get('section_selector') or '.detaildropdown__section, .detail-section, .detail-section__item, .metadata__item, .detaildropdown__row, dl'
                label_sel = config.get('label_selector') or 'h4'
                for sel in [s.strip() for s in section_sel.split(',') if s.strip()]:
                    for sec in soup.select(sel):
                        lbl = sec.select_one(label_sel) or sec.find(['strong', 'b'])
                        if not lbl:
                            continue
                        label = clean(lbl.get_text())
                        if not label:
                            continue
                        value = value_of(sec, config.get('value_selector'))
                        if not value:
                            clone_text = ''.join([x.get_text() for x in sec.find_all(text=True)])
                            try:
                                clone_text = clone_text.replace(lbl.get_text(), '')
                            except Exception:
                                pass
                            value = clean(clone_text)
                        put(label, value)
                # fallback dl
                try:
                    for dl in soup.select('dl'):
                        dts = dl.find_all('dt')
                        dds = dl.find_all('dd')
                        n = min(len(dts), len(dds))
                        for i in range(n):
                            put(dts[i].get_text(), dds[i].get_text())
                except Exception:
                    pass

            # cache and return
            _OVERVIEW_HTTP_CACHE[cache_key] = (time.time(), overview)
            return overview
        except Exception:
            return {}

