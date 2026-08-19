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


@tools.action(
    description='生成并跳转到 IDP 官方搜索结果页,避免 agent 手拼 URL 时把 page=21 写成 page=2D,term 写成 china%2Otemple 等脏参数.',
    param_model=NavigateIdpSearchPageParams,
)
async def navigate_idp_search_page(params: NavigateIdpSearchPageParams, browser_session):
    keyword = re.sub(r'\s+', ' ', str(params.keyword or 'china temple')).strip() or 'china temple'
    page = _coerce_int(params.page, default=1, minimum=1, maximum=999)
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
    return ActionResult(
        extracted_content=(
            f'✅ 已跳转到 IDP 搜索结果页\n'
            f'- keyword: {keyword}\n'
            f'- page: {page}\n'
            f'- limit: {limit}\n'
            f'- url: {url}'
        ),
        include_in_memory=True,
        long_term_memory=f'IDP 搜索页已跳转: page={page}, keyword={keyword}',
    )
