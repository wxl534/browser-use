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

        for (const hf of (cfg.header_fields || [])) {
            if (!hf || !hf.selector || !hf.label) continue;
            const el = doc.querySelector(hf.selector);
            if (el) put(hf.label, el.textContent);
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
            const sectionSel = cfg.section_selector;
            const labelSel = cfg.label_selector || 'h4';
            if (sectionSel) {
                for (const sec of doc.querySelectorAll(sectionSel)) {
                    const lbl = sec.querySelector(labelSel);
                    if (!lbl) continue;
                    const label = clean(lbl.textContent);
                    if (!label) continue;
                    let value = valueOf(sec, cfg.value_selector);
                    if (!value) {
                        const clone = sec.cloneNode(true);
                        const lc = clone.querySelector(labelSel);
                        if (lc) lc.remove();
                        value = clean(clone.textContent);
                    }
                    put(label, value);
                }
            }
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

    任何失败(无 URL / config 无效 / 浏览器异常 / HTTP 错误 / 解析失败)都优雅
    返回 ``{}``,绝不抛出,以免影响图片下载主流程.
    """
    detail_url = str(detail_url or '').strip()
    if not detail_url or not overview_config_is_active(config):
        return {}
    try:
        data = await evaluate_js_in_browser(
            browser_session, build_detail_overview_js(detail_url, config)
        )
    except Exception:
        return {}
    if not isinstance(data, dict) or not data.get('success'):
        return {}
    overview = data.get('overview') or {}
    if not isinstance(overview, dict):
        return {}
    return {str(k): str(v) for k, v in overview.items() if str(k).strip() and str(v).strip()}
