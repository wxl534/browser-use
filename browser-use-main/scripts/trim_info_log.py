"""
按用户粘贴的一条日志行,裁剪 info.log 前面的所有内容.

使用方式:
    python trim_info_log.py

脚本会提示你粘贴一条来自 info.log 的,包含时间戳的完整日志行.
找到后,会保留该行以及其后面的所有内容,并删除它前面的所有内容.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = BASE_DIR / 'info.log'


def normalize_pasted_line(line: str) -> str:
    """
    规范化用户粘贴的日志行.
    """
    return line.replace('\r\n', '\n').replace('\r', '\n').strip('\n').strip()


def find_line_index(lines: list[str], pasted_line: str) -> int:
    """
    在日志行列表中查找匹配的行号.

    优先精确匹配整行;如果失败,再尝试用去首尾空白后的文本匹配.
    """
    normalized_target = normalize_pasted_line(pasted_line)
    if not normalized_target:
        raise ValueError('输入不能为空')

    for index, line in enumerate(lines):
        if line.rstrip('\n') == normalized_target:
            return index

    for index, line in enumerate(lines):
        if line.strip() == normalized_target:
            return index

    raise ValueError('在 info.log 中未找到这条日志,请确认你粘贴的是文件中的完整单行日志.')


def trim_log_before_line(log_file: Path, pasted_line: str) -> int:
    """
    删除 log_file 中目标行之前的所有内容.

    Returns:
        被删除的行数.
    """
    if not log_file.exists():
        raise FileNotFoundError(f'日志文件不存在: {log_file}')

    lines = log_file.read_text(encoding='utf-8').splitlines(keepends=True)
    target_index = find_line_index(lines, pasted_line)
    trimmed_lines = lines[target_index:]
    log_file.write_text(''.join(trimmed_lines), encoding='utf-8')
    return target_index


def main() -> None:
    print('=' * 60)
    print('[工具] 裁剪 info.log 前置内容')
    print('=' * 60)
    print(f'[信息] 目标日志文件: {DEFAULT_LOG_FILE}')
    print('[提示] 请粘贴一条来自 info.log 的完整单行日志(必须包含时间戳)')

    pasted_line = input('> ').strip()
    try:
        removed_count = trim_log_before_line(DEFAULT_LOG_FILE, pasted_line)
    except Exception as e:
        print(f'[错误] 处理失败: {e}')
        raise SystemExit(1)

    print(f'[成功] 已删除目标日志前面的 {removed_count} 行内容')
    print('[成功] 目标日志行及其后续内容已保留')


if __name__ == '__main__':
    main()
