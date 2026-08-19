#!/usr/bin/env python
"""方案 C - Cloudflare 通行证取证脚本(独立运行,使用 Scrapling 环境).

用 Scrapling 的 StealthyFetcher(patchright + browserforge 指纹,能自动通过
Cloudflare Turnstile)访问目标站,取得 ``cf_clearance`` 等 cookie,导出成
browser-use 的 ``storage_state`` JSON.随后 main.py 通过 ``IDP_STORAGE_STATE``
注入这些 cookie,揣着"已验证通行证"进站,跳过人机验证.

关键约束(务必理解):
  * ``cf_clearance`` 与签发它的[出口 IP]绑定 -- 本脚本与 browser-use 必须走
    同一出口 IP(都直连,或都设同一个 IDP_PROXY_SERVER).
  * ``cf_clearance`` 会过期(常见 30 分钟~数小时)-- 失效后重跑本脚本刷新.

运行环境:本脚本依赖 scrapling,需用安装了 scrapling[fetchers] 的解释器运行
(与 browser-use 的 venv 隔离),例如 Scrapling 仓库下的 .venv:
    <Scrapling>/.venv/Scripts/python.exe fetch_cf_cookie.py

环境变量(与 main.py 共用同名变量,保证 IP/配置一致):
  IDP_CF_URL          取证目标 URL(默认 https://idp.bl.uk/)
  IDP_STORAGE_STATE   输出 storage_state 文件路径(默认脚本目录下 cf_storage.json)
  IDP_PROXY_SERVER    代理服务器,如 http://host:port 或 http://user:pass@host:port
  IDP_PROXY_USERNAME  代理用户名(可选;也可直接写进 IDP_PROXY_SERVER 的 URL)
  IDP_PROXY_PASSWORD  代理密码(可选)
  IDP_CF_HEADLESS     是否无头运行取证浏览器(默认 false:有窗口但全自动,无需人工点击)
  IDP_CF_TIMEOUT_MS   单步超时毫秒(默认 60000)

退出码:0 = 成功拿到 cf_clearance;2 = 请求成功但无 cf_clearance(多半是 IP 信誉
被挡,需换住宅代理);1 = 取证过程异常.供 supervisor 据此决定是否重试/换 IP.
"""

import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_URL = 'https://idp.bl.uk/'
DEFAULT_STORAGE = BASE_DIR / 'cf_storage.json'

# Playwright/storage_state 仅接受这三个 sameSite 取值.
_VALID_SAMESITE = {'Strict', 'Lax', 'None'}


def _env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def _truthy(value: str) -> bool:
    return value.lower() in {'1', 'true', 'yes', 'on'}


def _build_proxy():
    """从 IDP_PROXY_* 环境变量构造 Scrapling 接受的代理参数(与 main.py 同源).

    返回 None(不用代理),str(无认证)或 dict(带认证).
    """
    server = _env('IDP_PROXY_SERVER')
    if not server:
        return None
    username = _env('IDP_PROXY_USERNAME')
    password = _env('IDP_PROXY_PASSWORD')
    if username or password:
        return {'server': server, 'username': username, 'password': password}
    return server


def _normalize_cookies(raw_cookies) -> list:
    """把 Scrapling/Playwright 的 cookies 规整成 storage_state 的 cookie 列表."""
    cookies = []
    for c in raw_cookies or ():
        c = dict(c)
        same_site = c.get('sameSite', 'Lax')
        if same_site not in _VALID_SAMESITE:
            same_site = 'Lax'
        cookies.append(
            {
                'name': c.get('name', ''),
                'value': c.get('value', ''),
                'domain': c.get('domain', ''),
                'path': c.get('path', '/'),
                'expires': c.get('expires', -1),
                'httpOnly': bool(c.get('httpOnly', False)),
                'secure': bool(c.get('secure', False)),
                'sameSite': same_site,
            }
        )
    return cookies


def _cf_clearance_expiry(cookies) -> float | None:
    for c in cookies:
        if c['name'] == 'cf_clearance':
            exp = c.get('expires', -1)
            return exp if isinstance(exp, (int, float)) and exp > 0 else None
    return None


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else (_env('IDP_CF_URL') or DEFAULT_URL)
    out_path = Path(_env('IDP_STORAGE_STATE') or DEFAULT_STORAGE)
    headless = _truthy(_env('IDP_CF_HEADLESS', 'false'))
    timeout_ms = int(_env('IDP_CF_TIMEOUT_MS', '60000') or '60000')
    proxy = _build_proxy()

    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError as exc:
        print(
            '❌ 无法导入 scrapling.请用安装了 scrapling[fetchers] 的解释器运行本脚本,'
            f'当前解释器：{sys.executable}\n     原始错误：{exc}'
        )
        return 1

    print('🛡️  方案C 取证开始:用 Scrapling StealthyFetcher 通过 Cloudflare')
    print(f'     url={url}')
    print(f'     headless={headless}  timeout_ms={timeout_ms}')
    if proxy:
        shown = proxy if isinstance(proxy, str) else proxy.get('server')
        print(f'     proxy={shown}（取证与 browser-use 必须同一出口 IP，cf_clearance 才有效）')
    else:
        print('     proxy=(直连,未设 IDP_PROXY_SERVER)')

    started = time.time()
    try:
        response = StealthyFetcher.fetch(
            url,
            headless=headless,
            solve_cloudflare=True,
            network_idle=True,
            timeout=timeout_ms,
            proxy=proxy,
        )
    except Exception as exc:  # noqa: BLE001 - 取证可能因网络/反爬各种原因失败
        print(f'❌ 取证过程异常：{type(exc).__name__}: {exc}')
        return 1

    status = getattr(response, 'status', None)
    cookies = _normalize_cookies(getattr(response, 'cookies', ()))
    has_cf = any(c['name'] == 'cf_clearance' for c in cookies)
    expiry = _cf_clearance_expiry(cookies)
    # 记录取证时实际使用的 User-Agent -- cf_clearance 轻度绑指纹/UA,注入时让
    # browser-use 用同一个 UA,避免 Cloudflare 因 UA 不一致而拒收通行证.
    request_headers = getattr(response, 'request_headers', None) or {}
    user_agent = request_headers.get('user-agent') or request_headers.get('User-Agent')

    storage_state = {
        'cookies': cookies,
        'origins': [],
        # 自定义元数据:仅供人/工具查看,browser-use 加载时会忽略未知键.
        '_meta': {
            'source': 'scrapling.StealthyFetcher',
            'url': url,
            'status': status,
            'user_agent': user_agent,
            'fetched_at': int(started),
            'fetched_at_iso': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started)),
            'cf_clearance_present': has_cf,
            'cf_clearance_expires': expiry,
            'proxy': (proxy if isinstance(proxy, str) else (proxy or {}).get('server')) if proxy else None,
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(storage_state, indent=2, ensure_ascii=False), encoding='utf-8')

    elapsed = time.time() - started
    print(f'     HTTP status={status}  cookies={len(cookies)}  用时={elapsed:.1f}s')
    print(f'     User-Agent={user_agent or "(未捕获)"}')
    print(f'     写入 storage_state → {out_path}')

    if has_cf:
        when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry)) if expiry else '未知'
        print(f'✅ 已取得 cf_clearance（过期约 {when}）。main.py 设 IDP_STORAGE_STATE={out_path} 即可揣证进站。')
        return 0

    print(
        '⚠️  请求完成但未取得 cf_clearance.常见原因:目标站这次没弹 Turnstile(可能本就放行,'
        '可直接试跑),或被 IP 信誉层拦截 -- 后者需改用住宅代理(设 IDP_PROXY_SERVER 后重试).'
    )
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
