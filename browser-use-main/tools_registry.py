"""
工具注册模块 - 自定义 browser-use Agent 工具

将工具定义从 main.py 中提取出来，便于管理和复用。
包含：
- Pydantic 参数模型
- Tools 实例和注册
- extract_page_to_markdown 自定义 action
"""

import re
import json as json_module
import os
from pathlib import Path

from pydantic import BaseModel
from browser_use import Tools, ActionResult


# 使用脚本所在目录作为基准路径
BASE_DIR = Path(__file__).resolve().parent


# === 定义参数模型 ===

class ExtractPageContentParams(BaseModel):
    """提取网页内容的参数模型"""
    output_filename: str = "page_content.md"
    output_dir: str = str(Path(__file__).resolve().parent / "image")
    format_type: str = "markdown"  # markdown, json, text
    information_file_path: str = str(Path(__file__).resolve().parent / "Information.md")


# === 创建 tools 对象 ===
tools = Tools()
registry = tools.registry


# === 路径安全验证 ===

# 允许访问的基础目录（基于项目位置）
ALLOWED_BASE_DIRS = [
    Path(__file__).resolve().parent,
    Path(os.environ.get('BROWSER_USE_DOWNLOAD_DIR', str(Path.home() / 'Downloads'))),
]


def _is_path_allowed(target_path: str, allowed_bases: list[Path] = ALLOWED_BASE_DIRS) -> bool:
    """
    验证目标路径是否在允许的基础目录下，
    防止 LLM 通过工具参数读写任意文件。
    """
    try:
        resolved = Path(target_path).resolve()
        return any(
            resolved == base.resolve() or resolved.is_relative_to(base.resolve())
            for base in allowed_bases
        )
    except (ValueError, OSError):
        return False


# === 注册自定义动作 ===

@tools.action(
    description='提取当前网页源代码中符合Information.md文件中HTML代码块首尾行的部分并保存为文件',
    param_model=ExtractPageContentParams,
)
async def extract_page_to_markdown(params: ExtractPageContentParams, browser_session):
    """
    使用JavaScript提取网页源代码中符合Information.md文件中HTML代码块首尾行的部分。

    参数说明:
    - output_filename: 输出文件名
    - output_dir: 输出目录
    - format_type: 格式类型，可选 'markdown'/'json'/'text'
    - information_file_path: Information.md文件路径
    """
    try:
        # 路径安全检查
        if not _is_path_allowed(params.information_file_path):
            return ActionResult(error=f"路径不在允许范围内: {params.information_file_path}")
        if not _is_path_allowed(params.output_dir):
            return ActionResult(error=f"输出目录不在允许范围内: {params.output_dir}")

        # 读取 Information.md 文件内容
        info_file_path = Path(params.information_file_path)
        if not info_file_path.exists():
            return ActionResult(error=f"Information.md文件不存在: {params.information_file_path}")

        info_content = info_file_path.read_text(encoding="utf-8")
        # 统一换行符，避免 CRLF 导致正则不匹配
        info_content = info_content.replace('\r\n', '\n').replace('\r', '\n')

        # 提取 HTML 代码块的开始和结束行
        html_blocks = re.findall(r"```html\n([\s\S]*?)```", info_content)

        if not html_blocks:
            return ActionResult(error="Information.md中没有找到HTML代码块")

        # 为每个 HTML 块构建查找模式
        search_patterns = []
        for block in html_blocks:
            lines = block.strip().split("\n")
            if len(lines) >= 1:
                first_line = lines[0].strip()
                last_line = lines[-1].strip()
                if first_line and last_line:
                    search_patterns.append({
                        "start": first_line,
                        "end": last_line,
                        "full_block": block,
                    })

        if not search_patterns:
            return ActionResult(error="未能从HTML代码块中提取有效的首尾行")

        # JavaScript：获取网页源代码并查找匹配的代码块
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
        safe_filename = re.sub(r'[<>:"/\\|?*]', '_', params.output_filename)
        if not safe_filename.endswith(file_ext):
            safe_filename = re.sub(r'\.(md|json|txt)$', '', safe_filename) + file_ext

        # 构建完整路径
        output_dir = Path(params.output_dir)
        output_path = output_dir / safe_filename
        output_dir.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(file_content)

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


def _format_output(
    found_blocks: list[dict],
    page_title: str,
    page_url: str,
    format_type: str,
) -> tuple[str, str]:
    """根据格式类型生成输出内容，返回 (content, file_extension)。"""
    fmt = format_type.lower()

    if fmt == 'json':
        content = json_module.dumps(
            {
                'page_title': page_title,
                'url': page_url,
                'total_found_blocks': len(found_blocks),
                'found_blocks': found_blocks,
            },
            ensure_ascii=False,
            indent=2,
        )
        return content, '.json'

    if fmt == 'text':
        lines = [
            f"页面标题: {page_title}",
            f"URL: {page_url}",
            f"找到 {len(found_blocks)} 个匹配的HTML代码块",
            "=" * 80,
            "",
        ]
        for i, block in enumerate(found_blocks, 1):
            lines.append(f"--- 匹配块 {i} ---")
            lines.append(f"原始起始行: {block.get('original_start', '')}")
            lines.append(f"原始结束行: {block.get('original_end', '')}")
            lines.append("提取的HTML代码:")
            lines.append(block.get('content', ''))
            lines.append("")
        return "\n".join(lines), '.txt'

    # markdown（默认）
    md = f"# {page_title}\n\n"
    md += f"**URL**: {page_url}\n\n"
    md += f"**找到匹配的HTML代码块数量**: {len(found_blocks)}\n\n"
    md += "---\n\n"
    for i, block in enumerate(found_blocks, 1):
        md += f"## 匹配块 {i}\n\n"
        md += f"**原始起始行**: `{block.get('original_start', '')}`\n\n"
        md += f"**原始结束行**: `{block.get('original_end', '')}`\n\n"
        md += f"**提取的HTML代码**:\n```html\n{block.get('content', '')}\n```\n\n"
    return md, '.md'
