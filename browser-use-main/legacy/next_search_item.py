"""`next_search_item` 工具:统一发号"下一个该处理的 item 序号 + URL".

解决逐 item 通用下载流程没有页内序号游标的缺口:进入搜索结果页后,按 DOM 顺序
(左→右,上→下)枚举本页所有 item,以 image_record.jsonl(真实下载记录)+ 游标文件
为"已处理"事实来源,返回 DOM 顺序里第一个未处理的 item,LLM 不再靠记忆,避免错位 /
跳过 / 重复循环.站点差异通过 register_download_site_hint 的 item_link_selector 注册.

共享 helper / 参数模型由 tools_registry 提供;运行时全局通过 tr.* 实时读取.
"""
import tools_registry as tr
from tools_registry import (
    ActionResult,
    NextSearchItemParams,
    _clean_url_text,
    _current_browser_url,
    _enumerate_current_page_items,
    _select_next_search_item,
    _site_item_selector,
    tools,
)


@tools.action(
    description=(
        '搜索结果页"统一发号"工具:在当前搜索结果页按 DOM 顺序(从左到右,从上到下)枚举所有 item,'
        '结合 image_record.jsonl 已下载记录与游标文件,返回"下一个该处理的 item 序号 + 详情页 URL",'
        '确保逐 item 下载流程不会错位,跳过或重复循环.处理完一个 item 后再次调用并传入 mark_done_url '
        '即可领下一个;本页全部处理完会提示翻页.站点的 item 链接选择器可通过 item_selector 传入,'
        'IDP 等已注册站点会自动识别.'
    ),
    param_model=NextSearchItemParams,
)
async def next_search_item(params: NextSearchItemParams, browser_session):
    current_url = await _current_browser_url(browser_session)
    if not current_url:
        return ActionResult(error='无法获取当前浏览器页面 URL,请先跳转到搜索结果页.')

    selector = (params.item_selector or '').strip() or _site_item_selector(current_url)
    if not selector:
        return ActionResult(
            error=(
                f'当前站点（{current_url}）未注册 item 详情链接选择器，且未通过 item_selector 传入。\n'
                '请传入 item_selector(如 a[href*="/item/"]),或用 register_download_site_hint 为该站点注册 item_link_selector.'
            )
        )

    enum_result = await _enumerate_current_page_items(browser_session, selector, params.max_scan)
    if not enum_result.get('success'):
        return ActionResult(
            error=f'枚举当前页 item 失败: {enum_result.get("error", "未知错误")}（selector={selector}）'
        )

    items = enum_result.get('items') or []
    if not items:
        return ActionResult(
            extracted_content=(
                f'⚠️ 当前页用 selector `{selector}` 没有枚举到任何 item。\n'
                f'- 页面 URL: {current_url}\n'
                '可能尚未加载完成,选择器不匹配,或这不是搜索结果列表页.'
            ),
            include_in_memory=True,
        )

    selection = _select_next_search_item(
        items=items,
        current_page_url=current_url,
        keyword=(params.keyword or '').strip(),
        mark_done_url=_clean_url_text(params.mark_done_url),
        record_filename=params.record_filename,
    )

    total = selection['total_found']
    processed = selection['processed_count']
    next_item = selection['next_item']

    if next_item is None:
        return ActionResult(
            extracted_content=(
                f'✅ 本页 {total} 个 item 已全部处理完（已处理 {processed}/{total}）。\n'
                f'- 页面 URL: {current_url}\n'
                '请翻到下一页(navigate_idp_search_page 或点击下一页),再次调用 next_search_item 继续.'
            ),
            include_in_memory=True,
            long_term_memory=f'搜索页已全部处理: {current_url}（{total} 个 item）',
        )

    seq = selection['next_index'] + 1
    title = (next_item.get('title') or '').strip()
    return ActionResult(
        extracted_content=(
            f'➡️ 下一个该处理的 item：第 {seq}/{total} 个（已处理 {processed}/{total}）\n'
            f'- 标题: {title or "(无标题)"}\n'
            f'- 详情页 URL: {next_item["url"]}\n'
            '请点开/跳转到该 URL,提取图片信息并用 download_image_from_url 下载;'
            '处理完后再次调用 next_search_item 并把本次的 URL 作为 mark_done_url 传入,领取下一个.'
        ),
        include_in_memory=True,
        long_term_memory=f'下一个 item: 第 {seq}/{total} 个 {next_item["url"]}',
    )
