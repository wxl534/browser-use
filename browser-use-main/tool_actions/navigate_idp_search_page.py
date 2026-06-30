"""`navigate_idp_search_page` 工具:从 tools_registry.py 拆分而来.

共享 helper / 参数模型仍由 tools_registry 提供;运行时全局通过 tr.* 实时读取.
"""
import tools_registry as tr
from tools_registry import (
    ActionResult,
    IDPAdapter,
    NavigateIdpSearchPageParams,
    _coerce_int,
    _load_idp_progress,
    _navigate_to_image_url,
    _write_idp_progress,
    datetime,
    re,
    timezone,
    tools,
)


# 防跳页守卫:页码推进由确定性队列(idp_page_progress.json + batch_download/worker)负责,
# agent 只能在已知 current_page 基础上顺序 +1,禁止跳到 page 500/905 这类极深页。
GUARD_MAX_PAGE = 200


def _guard_idp_page(requested: int) -> tuple[int, int]:
    """把 LLM 请求的页码夹到 [1, min(current_page + 1, GUARD_MAX_PAGE)]。

    返回 (允许的页码, 参考的 current_page)。current_page 取自 idp_progress.json
    (worker 续跑时由确定性队列 select_next_page 写入,运行中由本工具每次更新),
    因此正常续跑能从深页起步,而 agent 无法在单次跳转里跨越多页。
    """
    prev = _load_idp_progress()
    try:
        current_page = int(prev.get('current_page') or 0)
    except (TypeError, ValueError):
        current_page = 0
    ceiling = (current_page + 1) if current_page > 0 else GUARD_MAX_PAGE
    allowed = max(1, min(requested, ceiling, GUARD_MAX_PAGE))
    return allowed, current_page


@tools.action(
    description='生成并跳转到 IDP 官方搜索结果页,避免 agent 手拼 URL 时把 page=21 写成 page=2D,term 写成 china%2Otemple 等脏参数.页码只能在当前页基础上顺序 +1,禁止跳到极深页.',
    param_model=NavigateIdpSearchPageParams,
)
async def navigate_idp_search_page(params: NavigateIdpSearchPageParams, browser_session):
    keyword = re.sub(r'\s+', ' ', str(params.keyword or 'china temple')).strip() or 'china temple'
    requested = _coerce_int(params.page, default=1, minimum=1, maximum=999)
    page, current_page = _guard_idp_page(requested)
    redirected = page != requested
    limit = _coerce_int(params.limit, default=50, minimum=1, maximum=100)
    adapter = IDPAdapter()
    url = adapter.build_search_url(keyword, page, limit)
    await _navigate_to_image_url(browser_session, url)
    _write_idp_progress({
        **_load_idp_progress(),
        'keyword': keyword,
        'current_page': page,
        'next_page': page,
        'next_index': 0,
        'limit': limit,
        'last_search_url': url,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    guard_note = ''
    if redirected:
        guard_note = (
            f'\n⚠️ 已忽略请求的 page={requested}:不允许跳页,'
            f'页码只能从 current_page={current_page} 顺序 +1(封顶 {GUARD_MAX_PAGE})。'
            f'某页 0 新增时由确定性队列自动推进/续跑,请勿手动跳到极深页。'
        )
    return ActionResult(
        extracted_content=(
            f'✅ 已跳转到 IDP 搜索结果页\n'
            f'- keyword: {keyword}\n'
            f'- page: {page}\n'
            f'- limit: {limit}\n'
            f'- url: {url}'
            f'{guard_note}'
        ),
        include_in_memory=True,
        long_term_memory=(
            f'IDP 搜索页已跳转: page={page}, keyword={keyword}'
            + (f'(请求的 page={requested} 因禁止跳页被改为 {page})' if redirected else '')
        ),
    )
