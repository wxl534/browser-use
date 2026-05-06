"""
select_download_format 工具单元测试（不使用大模型）

测试策略：
- Mock browser_session 和 CDP 会话
- 模拟 JavaScript 返回不同结果，验证工具的各分支逻辑
- 验证工具注册是否成功

运行方式：
    python test_select_download_format.py
    或
    python -m pytest test_select_download_format.py -v
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# 项目根目录
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from tools_registry import (
    SelectDownloadFormatParams,
    select_download_format,
    tools,
    registry,
)


def run_async(coro):
    """辅助函数：运行 async 函数"""
    return asyncio.run(coro)


def make_mock_browser_session(js_return_value):
    """
    创建一个 mock 的 browser_session，
    其 CDP evaluate 返回指定的 JS 结果。
    """
    mock_session = AsyncMock()
    mock_cdp = AsyncMock()
    mock_cdp.session_id = "test-session-id"
    mock_cdp.cdp_client = MagicMock()
    mock_cdp.cdp_client.send = MagicMock()
    mock_cdp.cdp_client.send.Runtime = MagicMock()
    mock_cdp.cdp_client.send.Runtime.evaluate = AsyncMock(return_value={
        'result': {'value': js_return_value}
    })
    mock_session.get_or_create_cdp_session = AsyncMock(return_value=mock_cdp)
    return mock_session


# ============================================================
# 测试 1：工具注册验证
# ============================================================

def test_tool_registered():
    """验证 select_download_format 已注册到 tools.registry 中"""
    action_names = list(registry.registry.actions.keys())
    assert 'select_download_format' in action_names, (
        f"select_download_format 未注册! 已注册的工具: {action_names}"
    )
    print("✅ 测试 1 通过: select_download_format 已成功注册")


# ============================================================
# 测试 2：参数模型默认值
# ============================================================

def test_params_default():
    """验证 SelectDownloadFormatParams 默认值为 TIFF"""
    params = SelectDownloadFormatParams()
    assert params.preferred_format == "TIFF"
    
    params2 = SelectDownloadFormatParams(preferred_format="JPEG")
    assert params2.preferred_format == "JPEG"
    print("✅ 测试 2 通过: 参数模型默认值正确")


# ============================================================
# 测试 3：成功选择 TIFF 格式
# ============================================================

def test_success_tiff():
    """模拟成功选择 TIFF 格式并点击 Go"""
    js_result = {
        'success': True,
        'selected_format': 'TIFF',
        'selected_text': 'TIFF (36.1 MB)',
        'download_url': 'https://tile.loc.gov/storage-services/master/afc/test.tif',
        'go_clicked': True,
        'available_formats': [
            {'index': 0, 'format': 'JPEG', 'text': 'JPEG (330x223px)', 'value': 'url1'},
            {'index': 1, 'format': 'TIFF', 'text': 'TIFF (36.1 MB)', 'value': 'url2'},
        ]
    }
    
    mock_session = make_mock_browser_session(js_result)
    params = SelectDownloadFormatParams(preferred_format="TIFF")
    
    result = run_async(select_download_format(params=params, browser_session=mock_session))
    
    assert result.error is None, f"不应有错误，但得到: {result.error}"
    assert "已选择下载格式: TIFF" in result.extracted_content
    assert "点击Go按钮开始下载" in result.extracted_content
    assert result.include_in_memory is True
    print("✅ 测试 3 通过: 成功选择 TIFF 并点击 Go")


# ============================================================
# 测试 4：格式不存在（没有 TIFF）
# ============================================================

def test_format_not_found():
    """模拟页面没有 TIFF 选项的情况"""
    js_result = {
        'success': False,
        'error': '未找到格式: TIFF',
        'available_formats': [
            {'index': 0, 'format': 'JPEG', 'text': 'JPEG (330x223px)', 'value': 'url1'},
            {'index': 1, 'format': 'GIF', 'text': 'GIF (16.7 KB)', 'value': 'url2'},
        ]
    }
    
    mock_session = make_mock_browser_session(js_result)
    params = SelectDownloadFormatParams(preferred_format="TIFF")
    
    result = run_async(select_download_format(params=params, browser_session=mock_session))
    
    assert result.error is not None
    assert "未找到格式: TIFF" in result.error
    assert "JPEG" in result.error  # 应列出可用格式
    assert "GIF" in result.error
    print("✅ 测试 4 通过: 格式不存在时正确返回错误和可用列表")


# ============================================================
# 测试 5：页面中没有 select-resource 元素
# ============================================================

def test_no_select_element():
    """模拟页面没有下载选择器的情况"""
    js_result = {
        'success': False,
        'error': '页面中未找到下载格式选择器(select-resource)',
        'available_formats': []
    }
    
    mock_session = make_mock_browser_session(js_result)
    params = SelectDownloadFormatParams()
    
    result = run_async(select_download_format(params=params, browser_session=mock_session))
    
    assert result.error is not None
    assert "未找到下载格式选择器" in result.error
    print("✅ 测试 5 通过: 无选择器时正确返回错误")


# ============================================================
# 测试 6：成功选择但 Go 按钮不存在
# ============================================================

def test_success_no_go_button():
    """模拟成功选择格式但找不到 Go 按钮"""
    js_result = {
        'success': True,
        'selected_format': 'TIFF',
        'selected_text': 'TIFF (39.3 MB)',
        'download_url': 'https://tile.loc.gov/test.tif',
        'go_clicked': False,
        'available_formats': []
    }
    
    mock_session = make_mock_browser_session(js_result)
    params = SelectDownloadFormatParams()
    
    result = run_async(select_download_format(params=params, browser_session=mock_session))
    
    assert result.error is None
    assert "未找到Go按钮，请手动点击" in result.extracted_content
    print("✅ 测试 6 通过: 无 Go 按钮时提示手动点击")


# ============================================================
# 测试 7：JavaScript 执行异常
# ============================================================

def test_js_exception():
    """模拟 JavaScript 执行时抛出异常"""
    mock_session = AsyncMock()
    mock_cdp = AsyncMock()
    mock_cdp.session_id = "test-session-id"
    mock_cdp.cdp_client = MagicMock()
    mock_cdp.cdp_client.send = MagicMock()
    mock_cdp.cdp_client.send.Runtime = MagicMock()
    mock_cdp.cdp_client.send.Runtime.evaluate = AsyncMock(return_value={
        'exceptionDetails': {'text': 'ReferenceError: document is not defined'}
    })
    mock_session.get_or_create_cdp_session = AsyncMock(return_value=mock_cdp)
    
    params = SelectDownloadFormatParams()
    result = run_async(select_download_format(params=params, browser_session=mock_session))
    
    assert result.error is not None
    assert "JavaScript执行失败" in result.error
    print("✅ 测试 7 通过: JS 异常时正确返回错误")


# ============================================================
# 测试 8：CDP 返回空数据
# ============================================================

def test_empty_response():
    """模拟 CDP 返回空数据"""
    mock_session = make_mock_browser_session(None)
    params = SelectDownloadFormatParams()
    
    result = run_async(select_download_format(params=params, browser_session=mock_session))
    
    assert result.error is not None
    assert "未获取到返回数据" in result.error
    print("✅ 测试 8 通过: 空数据时正确返回错误")


# ============================================================
# 测试 9：大小写不敏感
# ============================================================

def test_case_insensitive():
    """验证 preferred_format 大小写不敏感"""
    js_result = {
        'success': True,
        'selected_format': 'TIFF',
        'selected_text': 'TIFF (36.1 MB)',
        'download_url': 'https://example.com/test.tif',
        'go_clicked': True,
        'available_formats': []
    }
    
    mock_session = make_mock_browser_session(js_result)
    
    # 小写输入
    params = SelectDownloadFormatParams(preferred_format="tiff")
    result = run_async(select_download_format(params=params, browser_session=mock_session))
    
    assert result.error is None
    assert "TIFF" in result.extracted_content
    print("✅ 测试 9 通过: 大小写不敏感处理正确")


# ============================================================
# 测试 10：browser_session 连接异常
# ============================================================

def test_session_connection_error():
    """模拟 browser_session 连接失败"""
    mock_session = AsyncMock()
    mock_session.get_or_create_cdp_session = AsyncMock(
        side_effect=RuntimeError("Browser disconnected")
    )
    
    params = SelectDownloadFormatParams()
    result = run_async(select_download_format(params=params, browser_session=mock_session))
    
    assert result.error is not None
    assert "选择下载格式时出错" in result.error
    assert "Browser disconnected" in result.error
    print("✅ 测试 10 通过: 连接异常时正确捕获")


# ============================================================
# 主函数
# ============================================================

if __name__ == '__main__':
    tests = [
        test_tool_registered,
        test_params_default,
        test_success_tiff,
        test_format_not_found,
        test_no_select_element,
        test_success_no_go_button,
        test_js_exception,
        test_empty_response,
        test_case_insensitive,
        test_session_connection_error,
    ]
    
    passed = 0
    failed = 0
    
    print("=" * 60)
    print("select_download_format 工具测试")
    print("=" * 60)
    print()
    
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"❌ {test.__name__} 失败: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {test.__name__} 异常: {type(e).__name__}: {e}")
            failed += 1
    
    print()
    print("=" * 60)
    print(f"结果: {passed} 通过, {failed} 失败 (共 {len(tests)} 个测试)")
    print("=" * 60)
    
    sys.exit(0 if failed == 0 else 1)
