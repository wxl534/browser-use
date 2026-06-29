"""task.md 解析公共工具:关键词,目标数量,标题前缀.

worker.py(单轮)与 runner.py(监工)原本各自重复一套关键词/前缀解析,
统一收拢到此处,保证 task.md 的解析口径一致.
"""
import re


def extract_target_image_count(task: str, default: int = 1) -> int:
    """从 task.md 中提取目标图片数量,默认读取 `n = <number>`."""
    match = re.search(r'\bn\s*=\s*(\d+)\b', task, re.IGNORECASE)
    if not match:
        return default
    return max(1, int(match.group(1)))


def detect_search_keyword(task: str, default: str = 'china buddhist') -> str:
    """从 task.md 中提取搜索关键词;兼容标题/反引号/keyword="..." 多种写法."""
    patterns = [
        r'搜索关键词\s*\*\*`([^`]+)`\*\*',
        r'搜索关键词固定为\s*`([^`]+)`',
        r'关键词\s*`([^`]+)`',
        r'keyword="([^"]+)"',
        r"keyword='([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match and match.group(1).strip():
            return re.sub(r'\s+', ' ', match.group(1)).strip()
    return default


# worker.py 历史别名(关键词解析口径与 detect_search_keyword 一致).
extract_search_keyword = detect_search_keyword


def keyword_title_prefix(keyword: str) -> str:
    return re.sub(r'[^0-9A-Za-z]+', '_', keyword.lower()).strip('_') or 'idp_image'


# runner.py 历史别名.
title_prefix_from_keyword = keyword_title_prefix


def keyword_changed(old_keyword: str, new_keyword: str) -> bool:
    return keyword_title_prefix(old_keyword) != keyword_title_prefix(new_keyword)


def sanitize_run_folder_name(keyword: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', keyword_title_prefix(keyword))
    name = re.sub(r'_+', '_', name).strip('._ ')[:120]
    return name or 'idp_run'
