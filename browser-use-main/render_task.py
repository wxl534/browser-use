#!/usr/bin/env python
"""把 task_template.md + task_config.json 渲染成 task.md。

设计目标：task.md 不再手改。要改搜索词 / 目标值 / 站点 / 模式 / force_generic，
只改 task_config.json（或用本脚本的命令行参数覆盖），然后重新渲染。

单一真相源：task_config.json。字段：
  keyword         搜索关键词
  target_count    目标有效下载总数
  site_url        目标站点首页 URL
  site_label      报告/标题里用的站点显示名（留空自动按 site_url 主机名推导）
  allowed_hosts   允许下载的域名后缀白名单
  mode            "idp_batch"（站点专属批量）| "generic_per_item"（任意站点逐 item 稳定路径）
  force_generic   通用下载是否跳过站点专属 manifest 加速（稳定优先）
  item_selector   非注册站点的 item 详情链接 CSS 选择器（generic 模式才需要）

命令行（任一参数会覆盖 config 并写回 task_config.json）：
  python render_task.py                          # 用现有 config 渲染
  python render_task.py --keyword "india buddhist" --target 8000
  python render_task.py --mode generic_per_item --site https://example.org/ \
        --allowed-hosts example.org,img.example.org --force-generic
  python render_task.py --print                  # 渲染到 stdout，不写 task.md
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
TASK_FILE = BASE_DIR / 'task.md'
TEMPLATE_FILE = BASE_DIR / 'task_template.md'
CONFIG_FILE = BASE_DIR / 'task_config.json'

VALID_MODES = ('idp_batch', 'generic_per_item')

DEFAULT_CONFIG: dict = {
    'keyword': 'china buddhist',
    'target_count': 5000,
    'site_url': 'https://idp.bl.uk/',
    'site_label': '',
    'allowed_hosts': ['idp.bl.uk', 'data.idp.bl.uk', 'bl.uk'],
    'mode': 'idp_batch',
    'force_generic': False,
    'item_selector': '',
}


def title_prefix_from_keyword(keyword: str) -> str:
    return re.sub(r'[^0-9A-Za-z]+', '_', (keyword or '').lower()).strip('_') or 'idp_image'


def site_label_from_url(site_url: str) -> str:
    host = (urlparse((site_url or '').strip()).hostname or '').strip()
    return host or '目标站点'


def load_config(path: Path = CONFIG_FILE) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            for key in DEFAULT_CONFIG:
                if key in data:
                    cfg[key] = data[key]
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_FILE) -> None:
    persisted = {key: cfg[key] for key in DEFAULT_CONFIG}
    path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize_config(cfg: dict) -> dict:
    cfg = dict(cfg)
    cfg['keyword'] = re.sub(r'\s+', ' ', str(cfg.get('keyword', ''))).strip() or DEFAULT_CONFIG['keyword']
    try:
        cfg['target_count'] = max(1, int(cfg.get('target_count', DEFAULT_CONFIG['target_count'])))
    except (TypeError, ValueError):
        cfg['target_count'] = DEFAULT_CONFIG['target_count']
    cfg['site_url'] = str(cfg.get('site_url', '') or DEFAULT_CONFIG['site_url']).strip()
    if cfg.get('mode') not in VALID_MODES:
        cfg['mode'] = DEFAULT_CONFIG['mode']
    cfg['force_generic'] = bool(cfg.get('force_generic', False))
    hosts = cfg.get('allowed_hosts') or []
    if isinstance(hosts, str):
        hosts = [h.strip() for h in hosts.split(',')]
    cfg['allowed_hosts'] = [h.strip().lower() for h in hosts if h and h.strip()]
    cfg['item_selector'] = str(cfg.get('item_selector', '') or '').strip()
    cfg['site_label'] = str(cfg.get('site_label', '') or '').strip()
    return cfg


def build_context(cfg: dict) -> dict:
    cfg = normalize_config(cfg)
    ctx = dict(cfg)
    ctx['title_prefix'] = title_prefix_from_keyword(cfg['keyword'])
    ctx['site_label'] = cfg['site_label'] or site_label_from_url(cfg['site_url'])
    ctx['allowed_hosts_json'] = json.dumps(cfg['allowed_hosts'], ensure_ascii=False)
    ctx['is_batch'] = cfg['mode'] == 'idp_batch'
    ctx['is_generic'] = cfg['mode'] == 'generic_per_item'
    ctx['force_generic_value'] = 'true' if cfg['force_generic'] else 'false'
    return ctx


# 只匹配「最内层」if 块：块体内不得再含 {{#if 或 {{/if}}，配合下面的 while 循环
# 从内向外逐层求值，从而真正支持嵌套（避免惰性 .*? 把外层 if 与内层 {{/if}} 错配）。
# 注意：结尾不再吞换行——块级 if 自有前导换行即可，行内 if（如 ...{{/if}}\n下一行）
# 若吞掉结尾换行会把下一行并到本行，多余空行交由末尾 \n{3,}→\n\n 收敛。
_IF_BLOCK = re.compile(
    r'\{\{#if (\w+)\}\}\n?((?:(?!\{\{#if |\{\{/if\}\}).)*?)\{\{/if\}\}',
    re.DOTALL,
)
_VAR = re.compile(r'\{\{\s*(\w+)\s*\}\}')


def render(template: str, ctx: dict) -> str:
    """极简模板引擎：先按 {{#if key}}..{{/if}} 取舍块（从内向外逐层、循环到稳定，支持嵌套），再做 {{ var }} 替换。"""
    def strip_blocks(text: str) -> str:
        def repl(match: re.Match) -> str:
            key, body = match.group(1), match.group(2)
            return body if ctx.get(key) else ''
        prev = None
        while prev != text:
            prev = text
            text = _IF_BLOCK.sub(repl, text)
        return text

    def sub_vars(match: re.Match) -> str:
        return str(ctx.get(match.group(1), ''))

    text = strip_blocks(template)
    text = _VAR.sub(sub_vars, text)
    # 收敛多余空行（条件块删除后可能留下 3+ 连续换行）
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def render_to_task(
    cfg: dict | None = None,
    *,
    template_file: Path = TEMPLATE_FILE,
    output: Path = TASK_FILE,
) -> Path:
    if cfg is None:
        cfg = load_config()
    ctx = build_context(cfg)
    template = template_file.read_text(encoding='utf-8')
    output.write_text(render(template, ctx), encoding='utf-8')
    return output


def apply_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    cfg = dict(cfg)
    if args.keyword is not None:
        cfg['keyword'] = args.keyword
    if args.target is not None:
        cfg['target_count'] = args.target
    if args.site is not None:
        cfg['site_url'] = args.site
    if args.site_label is not None:
        cfg['site_label'] = args.site_label
    if args.allowed_hosts is not None:
        cfg['allowed_hosts'] = [h.strip() for h in args.allowed_hosts.split(',') if h.strip()]
    if args.mode is not None:
        cfg['mode'] = args.mode
    if args.item_selector is not None:
        cfg['item_selector'] = args.item_selector
    if args.force_generic is not None:
        cfg['force_generic'] = args.force_generic
    return normalize_config(cfg)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Render task.md from task_template.md + task_config.json')
    parser.add_argument('--keyword', type=str, default=None, help='搜索关键词')
    parser.add_argument('--target', type=int, default=None, help='目标有效下载总数')
    parser.add_argument('--site', type=str, default=None, help='目标站点首页 URL')
    parser.add_argument('--site-label', type=str, default=None, help='站点显示名（留空自动按主机名）')
    parser.add_argument('--allowed-hosts', type=str, default=None, help='允许域名后缀白名单，逗号分隔')
    parser.add_argument('--mode', choices=VALID_MODES, default=None, help='idp_batch | generic_per_item')
    parser.add_argument('--item-selector', type=str, default=None, help='非注册站点的 item 链接 CSS 选择器')
    fg = parser.add_mutually_exclusive_group()
    fg.add_argument('--force-generic', dest='force_generic', action='store_true', default=None,
                    help='通用下载跳过站点 manifest 加速（稳定优先）')
    fg.add_argument('--no-force-generic', dest='force_generic', action='store_false', default=None,
                    help='关闭 force_generic')
    parser.add_argument('--config', type=Path, default=CONFIG_FILE, help='task_config.json 路径')
    parser.add_argument('--template', type=Path, default=TEMPLATE_FILE, help='模板路径')
    parser.add_argument('--output', type=Path, default=TASK_FILE, help='输出 task.md 路径')
    parser.add_argument('--print', action='store_true', help='渲染到 stdout，不写文件、不改 config')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = apply_overrides(load_config(args.config), args)
    if args.print:
        print(render(args.template.read_text(encoding='utf-8'), build_context(cfg)))
        return 0
    save_config(cfg, args.config)
    out = render_to_task(cfg, template_file=args.template, output=args.output)
    print(json.dumps({
        'rendered': str(out),
        'config': str(args.config),
        'keyword': cfg['keyword'],
        'target_count': cfg['target_count'],
        'site_url': cfg['site_url'],
        'mode': cfg['mode'],
        'force_generic': cfg['force_generic'],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
