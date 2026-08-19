"""适配器解析:根据当前结果页 URL 选用合适的 SiteAdapter.

优先级:
1. 内置 IDPAdapter(idp.bl.uk).
2. site_profiles/*.json 里每个 profile 生成的 ConfigIIIFAdapter,按 is_results_url 命中.
3. 兜底仍返回 IDPAdapter,保证既有行为不变.

profile 目录用相对路径(相对本文件),不硬编码绝对地址.
"""
from __future__ import annotations

import json
import os
from typing import Any

from adapters.base import SiteAdapter
from adapters.generic_config import ConfigIIIFAdapter
from adapters.idp import IDPAdapter

_PROFILE_DIR = os.path.join(os.path.dirname(__file__), 'site_profiles')


def load_config_adapters() -> list[ConfigIIIFAdapter]:
    """读取 site_profiles/*.json,构造全部 ConfigIIIFAdapter.坏 profile 跳过."""
    adapters: list[ConfigIIIFAdapter] = []
    if not os.path.isdir(_PROFILE_DIR):
        return adapters
    for name in sorted(os.listdir(_PROFILE_DIR)):
        if not name.endswith('.json'):
            continue
        path = os.path.join(_PROFILE_DIR, name)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                profile = json.load(fh)
            adapters.append(ConfigIIIFAdapter(profile))
        except (OSError, ValueError):
            continue
    return adapters


def resolve_adapter(current_url: str) -> SiteAdapter:
    """按当前 URL 选 adapter;命中配置站点用 ConfigIIIFAdapter,否则回退 IDP."""
    idp = IDPAdapter()
    if current_url and idp.is_results_url(current_url):
        return idp
    for adapter in load_config_adapters():
        if current_url and adapter.is_results_url(current_url):
            return adapter
    return idp


async def resolve_adapter_for_session(browser_session: Any) -> SiteAdapter:
    """从浏览器会话读当前 URL 再解析 adapter."""
    from tools_registry import _current_browser_url
    try:
        current_url = await _current_browser_url(browser_session)
    except Exception:
        current_url = ''
    return resolve_adapter(current_url)
