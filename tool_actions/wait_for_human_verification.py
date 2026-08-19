"""`wait_for_human_verification` 工具:从 tools_registry.py 拆分而来.

共享 helper / 参数模型仍由 tools_registry 提供;运行时全局通过 tr.* 实时读取.
"""
import tools_registry as tr
from tools_registry import (
    ActionResult,
    WaitForHumanVerificationParams,
    _attempt_cloudflare_autoclick,
    _detect_human_verification,
    asyncio,
    tools,
)


@tools.action(
    description='检测当前页面是否为 Cloudflare/人机验证页;如果是,先尝试用 CDP 自动点击 Turnstile 复选框(仅对“单击放行”型有效),失败再等待用户在浏览器中手动完成验证后继续.',
    param_model=WaitForHumanVerificationParams,
)
async def wait_for_human_verification(params: WaitForHumanVerificationParams, browser_session):
    """
    优先自动点击 Cloudflare/Turnstile 复选框;无法自动通过(如交互式拼图)时回退人工等待.
    """
    try:
        deadline = asyncio.get_running_loop().time() + params.timeout_seconds
        first_state = await _detect_human_verification(browser_session)
        if not first_state.get('is_challenge'):
            msg = '✅ 当前页面未检测到 Cloudflare/人机验证,可以继续执行.'
            return ActionResult(extracted_content=msg, include_in_memory=True, long_term_memory='当前页面未检测到人机验证')

        auto_clicked = False
        if params.auto_click:
            auto_clicked = await _attempt_cloudflare_autoclick(
                browser_session, attempts=params.auto_click_attempts
            )
            if auto_clicked:
                state = await _detect_human_verification(browser_session)
                msg = (
                    '✅ 已自动点击通过 Cloudflare/人机验证,页面已恢复,可以继续处理队列.\n'
                    f"当前页面: {state.get('url', '')}\n"
                    f"标题: {state.get('title', '')}"
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory='人机验证已自动点击通过,可以继续任务',
                )

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(params.poll_interval_seconds)
            state = await _detect_human_verification(browser_session)
            if not state.get('is_challenge'):
                msg = (
                    '✅ 人机验证已完成,页面已恢复,可以继续处理队列.\n'
                    f"当前页面: {state.get('url', '')}\n"
                    f"标题: {state.get('title', '')}"
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory='人机验证已完成,可以继续任务',
                )

        msg = (
            '仍处于 Cloudflare/人机验证页面(自动点击未能通过,可能是交互式挑战).'
            '请在打开的浏览器中手动点击验证按钮并等待页面加载完成,'
            '然后再次调用 wait_for_human_verification 或继续当前队列项.\n'
            f"页面: {first_state.get('url', '')}\n"
            f"标题: {first_state.get('title', '')}\n"
            f"页面文本: {first_state.get('text_sample', '')}"
        )
        return ActionResult(error=msg)
    except Exception as e:
        return ActionResult(error=f'等待人机验证时出错: {str(e)}')
