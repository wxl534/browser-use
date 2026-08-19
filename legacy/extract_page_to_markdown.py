"""`extract_page_to_markdown` 工具:从 tools_registry.py 拆分而来.

共享 helper / 参数模型仍由 tools_registry 提供;运行时全局通过 tr.* 实时读取.
"""
import tools_registry as tr
from tools_registry import (
    ActionResult,
    ExtractPageContentParams,
    _format_output,
    _load_information_patterns,
    _resolve_extract_paths,
    _safe_extract_filename,
    _write_extracted_file,
    json_module,
    tools,
)


@tools.action(
    description='提取当前网页源代码中符合Information.md文件中HTML代码块首尾行的部分并保存为文件',
    param_model=ExtractPageContentParams,
)
async def extract_page_to_markdown(params: ExtractPageContentParams, browser_session):
    """
    使用JavaScript提取网页源代码中符合Information.md文件中HTML代码块首尾行的部分.

    参数说明:
    - output_filename: 输出文件名
    - output_dir: 输出目录
    - format_type: 格式类型,可选 'markdown'/'json'/'text'
    - information_file_path: Information.md文件路径
    """
    try:
        info_file_path, output_dir_path = _resolve_extract_paths(params)
        search_patterns = _load_information_patterns(info_file_path)

        # JavaScript:获取网页源代码并查找匹配的代码块
        js_code = f'''
        (function() {{
            try {{
                const fullHtml = document.documentElement.outerHTML;
                const searchPatterns = {json_module.dumps(search_patterns, ensure_ascii=False)};
                const foundBlocks = [];

                for (const pattern of searchPatterns) {{
                    const escapeRegExp = (string) => {{
                        return string.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\$&');
                    }};
                    const startPattern = escapeRegExp(pattern.start);
                    const endPattern = escapeRegExp(pattern.end);
                    const regex = new RegExp(startPattern + '[\\\\s\\\\S]*?' + endPattern, 'gi');

                    let match;
                    while ((match = regex.exec(fullHtml)) !== null) {{
                        const alreadyExists = foundBlocks.some(block => block.content === match[0]);
                        if (!alreadyExists) {{
                            foundBlocks.push({{
                                original_start: pattern.start,
                                original_end: pattern.end,
                                content: match[0],
                                position: match.index
                            }});
                        }}
                    }}
                }}

                return {{
                    success: true,
                    url: window.location.href,
                    title: document.title,
                    found_blocks: foundBlocks,
                    total_found: foundBlocks.length,
                    search_patterns: searchPatterns
                }};
            }} catch (error) {{
                return {{
                    success: false,
                    error: error.message,
                    stack: error.stack
                }};
            }}
        }})()
        '''

        # 执行 JavaScript
        cdp_session = await browser_session.get_or_create_cdp_session()
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
            session_id=cdp_session.session_id,
        )

        if result.get('exceptionDetails'):
            error_text = result['exceptionDetails'].get('text', '未知JS错误')
            return ActionResult(error=f'JavaScript执行失败: {error_text}')

        data = result.get('result', {}).get('value')
        if not data or not data.get('success'):
            error_msg = data.get('error', '未知错误') if data else '未获取到数据'
            return ActionResult(error=f'提取失败: {error_msg}')

        found_blocks = data.get('found_blocks', [])
        page_title = data.get('title', '')
        page_url = data.get('url', '')

        if not found_blocks:
            return ActionResult(error="在网页源代码中未找到匹配的HTML代码块")

        # 根据格式类型生成内容
        file_content, file_ext = _format_output(
            found_blocks, page_title, page_url, params.format_type
        )

        # 清理文件名中的非法字符
        safe_filename = _safe_extract_filename(params.output_filename, file_ext)

        # 构建完整路径(使用已验证的 output_dir)
        output_path = _write_extracted_file(output_dir_path, safe_filename, file_content)

        success_msg = (
            f"✅ 成功提取网页中匹配的HTML代码块并保存到: {output_path}\n"
            f"格式: {params.format_type.upper()}\n"
            f"共找到 {len(found_blocks)} 个匹配块"
        )

        return ActionResult(
            extracted_content=success_msg,
            include_in_memory=True,
            long_term_memory=f'已将当前网页中匹配Information.md的HTML代码块提取并保存到 {safe_filename} (格式: {params.format_type})',
        )

    except Exception as e:
        return ActionResult(error=f'提取网页内容时出错: {str(e)}')
