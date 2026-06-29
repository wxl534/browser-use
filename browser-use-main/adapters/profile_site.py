"""自动站点 profiler:读取一个搜索结果页的 DOM,启发式推断接入新站所需的 3 个未知量
(item 链接选择器,item-id 正则,IIIF manifest 模板),生成 site_profiles/<id>.json.

无需为结构类似的 IIIF 站点手写 Python adapter:跑一次本脚本拿到 profile,人工确认后
ConfigIIIFAdapter 直接接管批量下载.

用法(在一个已打开搜索结果页的浏览器会话里)::

    profile = await profile_results_page(browser_session, site_id='gallica')
    save_profile(profile)            # 写入 adapters/site_profiles/gallica.json

或离线对一段 HTML 试跑见 __main__.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

_PROFILE_DIR = os.path.join(os.path.dirname(__file__), 'site_profiles')

# 浏览器端启发式:统计重复出现的链接 path 形态,找最像 item 列表的那一类.
_PROFILE_JS = r'''
(function() {
    const groups = {};
    for (const a of document.querySelectorAll('a[href]')) {
        let u; try { u = new URL(a.href, location.href); } catch (_) { continue; }
        if (u.hostname !== location.hostname) continue;
        const tpl = u.pathname.replace(/\/[0-9a-fA-F]{6,}/g, '/{hex}').replace(/\/\d+/g, '/{num}');
        const g = groups[tpl] || (groups[tpl] = {count:0, samples:[]});
        g.count++; if (g.samples.length < 5) g.samples.push(u.href);
    }
    const manifests = [];
    for (const a of document.querySelectorAll('a[href*="manifest"],link[href*="manifest"]')) manifests.push(a.href);
    for (const m of (document.body.innerHTML.match(/https?:\/\/[^"'\s]*manifest[^"'\s]*/gi) || [])) manifests.push(m);
    return {host: location.hostname, groups, manifests: [...new Set(manifests)].slice(0,5), title: document.title};
})()
'''


def _infer_id_regex(samples: list[str]) -> tuple[str, str]:
    """从样本 URL 找出 item-id 段,返回 (regex, 示例id)."""
    for s in samples:
        path = urlparse(s).path
        m = re.search(r'/([0-9a-fA-F]{8,64})/?$', path)
        if m:
            return r'/([0-9a-fA-F]{8,64})', m.group(1)
        m = re.search(r'/(\d{3,})/?$', path)
        if m:
            return r'/(\d{3,})', m.group(1)
        m = re.search(r'/([\w.:/-]+)/?$', path)
        if m:
            return r'/([\w.:%-]+?)/?$', m.group(1)
    return r'/([\w-]+)/?$', ''


def build_profile(site_id: str, host: str, groups: dict, manifests: list[str]) -> dict:
    """从浏览器收集结果合成 profile(manifest 模板可能需人工补全 {id})."""
    best = max(groups.items(), key=lambda kv: kv[1]['count'], default=(None, {'samples': [], 'count': 0}))
    tpl, info = best
    samples = info.get('samples') or []
    selector = f"a[href*='{tpl.split('/{')[0]}']" if tpl and '/{' in tpl else "a[href]"
    regex, sample_id = _infer_id_regex(samples)
    manifest_tpl = ''
    for m in manifests:
        if sample_id and sample_id in m:
            manifest_tpl = m.replace(sample_id, '{id}')
            break
    return {
        'site_id': site_id, 'host_suffixes': [host], 'results_host': host,
        'item_link_selector': selector, 'item_id_regex': regex,
        'manifest_template': manifest_tpl or 'TODO: https://.../iiif/{id}/manifest.json',
        'keyword_param': 'q', 'page_param': 'page', 'limit_param': 'limit',
        'search_url_template': f'https://{host}/search?q={{keyword}}&page={{page}}&limit={{limit}}',
        '_sample_items': samples,
    }


async def profile_results_page(browser_session: Any, site_id: str) -> dict:
    cdp = await browser_session.get_or_create_cdp_session()
    res = await cdp.cdp_client.send.Runtime.evaluate(
        params={'expression': _PROFILE_JS, 'returnByValue': True}, session_id=cdp.session_id)
    data = res.get('result', {}).get('value') or {}
    return build_profile(site_id, data.get('host', ''), data.get('groups', {}), data.get('manifests', []))


def save_profile(profile: dict) -> str:
    os.makedirs(_PROFILE_DIR, exist_ok=True)
    path = os.path.join(_PROFILE_DIR, f"{profile['site_id']}.json")
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
    return path
