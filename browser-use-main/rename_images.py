"""
图片重命名脚本

功能：
1. 从 title.txt 文件读取标题
2. 扫描下载目录中的所有图片文件
3. 按顺序将标题分配给图片
4. 重命名图片文件为对应的标题

使用方法：
python rename_images.py
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# 使用脚本所在目录作为基准路径
BASE_DIR = Path(__file__).resolve().parent


def sanitize_filename(filename: str) -> str:
    """
    清理文件名中的非法字符（Windows 兼容）
    """
    # 先处理换行符（在替换空格之前）
    sanitized = filename.replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    
    # Windows 非法字符：< > : " / \ | ? *（包括全角问号）
    sanitized = re.sub(r'[<>:"/\\|?？*]', '_', sanitized)
    
    # 替换空白字符为下划线
    sanitized = sanitized.replace(' ', '_')
    
    # 合并连续下划线
    sanitized = re.sub(r'_+', '_', sanitized)
    
    # 移除前后下划线
    sanitized = sanitized.strip('_')
    
    # 限制长度（Windows 最大 255 字符，留余量给扩展名和冲突后缀）
    if len(sanitized) > 200:
        sanitized = sanitized[:200].rstrip('_')
    
    # 防止空文件名
    if not sanitized:
        sanitized = 'unnamed'
    
    return sanitized


def get_image_files(directory: Path) -> list[Path]:
    """
    获取目录中的所有图片文件，按修改时间排序
    
    Args:
        directory: 目标目录
        
    Returns:
        图片文件路径列表（按时间排序）
    """
    image_extensions = {'.jpg', '.jpeg', '.tiff', '.tif', '.png', '.gif', '.webp'}
    
    # 获取所有图片文件
    image_files = [
        f for f in directory.iterdir() 
        if f.is_file() and f.suffix.lower() in image_extensions
    ]
    
    # 按修改时间排序（最早的在前）
    image_files.sort(key=lambda x: x.stat().st_mtime)
    
    return image_files


def load_downloaded_records(record_file: Path) -> list[dict]:
    """
    读取 download_record.jsonl 中真正成功下载的记录。
    """
    if not record_file.exists():
        return []

    records: list[dict] = []
    for line in record_file.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get('status') == 'downloaded' and record.get('title'):
            records.append(record)
    return records


def _record_target_path(download_path: Path, record: dict) -> Path:
    """
    根据下载记录里的标题生成目标文件路径。
    """
    file_path = Path(str(record.get('file_path') or ''))
    suffix = file_path.suffix if file_path.suffix.lower() in {'.jpg', '.jpeg', '.tiff', '.tif', '.png', '.gif', '.webp'} else '.png'
    return download_path / f"{sanitize_filename(str(record.get('title') or 'untitled'))}{suffix}"


def _resolve_record_file(download_path: Path, record: dict, used_paths: set[Path]) -> Path | None:
    """
    用下载记录精确定位要重命名的文件。

    优先级：
    1. record.file_path 当前仍存在
    2. 目标标题文件已经存在（说明之前已正确重命名）
    3. image 目录中同名原始文件仍存在
    4. file_size 唯一匹配
    """
    raw_path = Path(str(record.get('file_path') or ''))
    candidates: list[Path] = []
    if raw_path.name:
        candidates.append(raw_path if raw_path.is_absolute() else download_path / raw_path)
        candidates.append(download_path / raw_path.name)

    target_path = _record_target_path(download_path, record)
    candidates.append(target_path)

    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.exists() and candidate.is_file() and candidate not in used_paths:
            return candidate

    file_size = record.get('file_size')
    if isinstance(file_size, int) and file_size > 0:
        size_matches = [
            path
            for path in get_image_files(download_path)
            if path.resolve() not in used_paths and path.stat().st_size == file_size
        ]
        if len(size_matches) == 1:
            return size_matches[0].resolve()

    return None


def _rename_with_conflict_handling(source: Path, target: Path) -> tuple[bool, Path, str | None]:
    """
    重命名文件，自动处理目标文件名冲突；如果已经是目标名则视为成功。
    """
    source = source.resolve()
    target = target.resolve()
    if source == target:
        return True, target, None

    final_target = target
    counter = 1
    while final_target.exists():
        final_target = target.with_name(f'{target.stem}_{counter}{target.suffix}')
        counter += 1

    try:
        source.rename(final_target)
    except OSError as exc:
        return False, source, str(exc)
    return True, final_target, None


def _sync_record_file_renames(record_file: Path, download_path: Path, rename_map: list[dict]) -> None:
    """
    重命名完成后同步结构化记录，避免恢复/校验时还指向 temple_### 临时名。
    """
    if not record_file.exists() or not rename_map:
        return

    by_sequence = {
        int(item['sequence']): item
        for item in rename_map
        if item.get('status') in {'renamed', 'already_named'} and str(item.get('sequence') or '').isdigit()
    }
    if not by_sequence:
        return

    updated_records: list[dict] = []
    changed = False
    for line in record_file.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            updated_records.append({'_raw': line})
            continue

        try:
            sequence = int(record.get('sequence') or 0)
        except (TypeError, ValueError):
            sequence = 0
        rename_item = by_sequence.get(sequence)
        if rename_item:
            old_name = str(record.get('file_name') or '')
            new_name = str(rename_item.get('new_name') or '')
            if new_name and old_name != new_name:
                record.setdefault('original_file_name', old_name)
                record['file_name'] = new_name
                record['final_file_name'] = new_name
                record['file_path'] = str((download_path / new_name).resolve())
                record['renamed_at'] = datetime.now(timezone.utc).isoformat()
                changed = True
        updated_records.append(record)

    if not changed:
        return

    backup = record_file.with_suffix(record_file.suffix + '.bak')
    if not backup.exists():
        backup.write_text(record_file.read_text(encoding='utf-8'), encoding='utf-8')
    with open(record_file, 'w', encoding='utf-8') as file:
        for record in updated_records:
            if '_raw' in record:
                file.write(record['_raw'] + '\n')
            else:
                file.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(f"[信息] 已同步结构化记录中的重命名文件名：{record_file}")


def rename_images_from_records(download_path: Path, record_file: Path) -> tuple[bool, list[dict]]:
    """
    优先用 download_record.jsonl 精确驱动重命名，避免 title.txt 顺序与文件系统顺序错位。
    """
    records = load_downloaded_records(record_file)
    if not records:
        return False, []

    print(f"\n[信息] 使用 {record_file.name} 精确重命名：{record_file}")
    print(f"[成功] 读取到 {len(records)} 条成功下载记录")

    used_paths: set[Path] = set()
    rename_map: list[dict] = []
    success_count = 0
    failed_count = 0

    for idx, record in enumerate(records, 1):
        title = str(record.get('title') or 'untitled')
        source = _resolve_record_file(download_path, record, used_paths)
        target = _record_target_path(download_path, record)

        if source is None:
            print(f"[警告] [{idx}/{len(records)}] 未找到可匹配文件：{title}")
            failed_count += 1
            rename_map.append({
                'index': idx,
                'sequence': record.get('sequence'),
                'old_name': '',
                'new_name': '',
                'title': title,
                'status': 'missing',
            })
            continue

        old_name = source.name
        ok, final_path, error = _rename_with_conflict_handling(source, target)
        used_paths.add(final_path.resolve())
        if ok:
            status = 'already_named' if old_name == final_path.name else 'renamed'
            print(f"[成功] [{idx}/{len(records)}] {old_name} → {final_path.name}")
            success_count += 1
            rename_map.append({
                'index': idx,
                'sequence': record.get('sequence'),
                'old_name': old_name,
                'new_name': final_path.name,
                'title': title,
                'status': status,
                'page_url': record.get('page_url', ''),
                'download_url': record.get('url', ''),
            })
        else:
            print(f"[错误] [{idx}/{len(records)}] 重命名失败：{old_name} - {error}")
            failed_count += 1
            rename_map.append({
                'index': idx,
                'sequence': record.get('sequence'),
                'old_name': old_name,
                'new_name': '',
                'title': title,
                'status': 'failed',
                'error': error or '',
            })

    print("\n" + "=" * 60)
    print("[统计] 记录驱动重命名完成")
    print(f"[成功] 成功/已正确命名：{success_count} 个")
    print(f"[错误] 缺失/失败：{failed_count} 个")
    print("=" * 60)
    _sync_record_file_renames(record_file, download_path, rename_map)
    return success_count > 0, rename_map


def find_structured_record_file(agent_data_dir: Path) -> Path:
    """
    优先使用通用图片记录，再回退到 LOC 下载记录。
    """
    for filename in ('image_record.jsonl', 'download_record.jsonl'):
        candidate = agent_data_dir / filename
        if load_downloaded_records(candidate):
            return candidate
    return agent_data_dir / 'download_record.jsonl'


def extract_content_from_log(log_file: Path, start_keyword: str, end_keyword: str = "END") -> list[str] | None:
    """
    从 info.log 文件中提取最新任务的内容
    
    查找关键行:start_keyword,然后提取后续行直到 end_keyword
    
    Args:
        log_file: info.log 文件路径
        start_keyword: 开始关键词 (如"Title.txt:" 或 "Title.txt:\\n")
        end_keyword: 结束关键词 (默认 "END")
        
    Returns:
        内容列表，如果未找到则返回 None
    """
    if not log_file.exists():
        return None
    
    print(f"[信息] 尝试从日志文件提取 {start_keyword} 内容:{log_file}")
    
    with open(log_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 处理可能的转义换行符 \n
    # 先查找包含关键词的行 (处理实际换行的情况)
    lines = content.split('\n')
    matches = []
    
    for idx, line in enumerate(lines):
        # 移除日志前缀 (时间戳等)
        clean_line = line.strip()
        if '→' in clean_line:
            clean_line = clean_line.split('→', 1)[1].strip()
        
        # 检查是否包含关键词 (支持多种格式)
        if start_keyword in clean_line or start_keyword.replace(':', '') in clean_line:
            matches.append({
                'line_number': idx,
                'full_line': clean_line
            })
    
    if not matches:
        print(f"[警告] 未在日志中找到关键词:{start_keyword}")
        # 尝试在整个内容中搜索 (处理 \n转义的情况)
        if '\\n' in content:
            print("[信息] 检测到转义换行符，尝试解析...")
            # 将 \n替换为实际换行后重新搜索
            normalized_content = content.replace('\\n', '\n')
            norm_lines = normalized_content.split('\n')
            for idx, line in enumerate(norm_lines):
                if start_keyword in line.strip():
                    matches.append({
                        'line_number': idx,
                        'full_line': line.strip()
                    })
    
    if not matches:
        return None
    
    # 使用最后一个（最新的）匹配
    latest_match = matches[-1]
    print(f"[成功] 找到最新 {start_keyword}（第 {latest_match['line_number']} 行）")
    
    # 提取内容 - 优先从完整行内容中提取
    contents = []
    
    # 首先尝试从找到的行直接提取 (处理关键词和内容在同一行的情况)
    full_line = latest_match.get('full_line', '')
    
    # 如果关键词后有内容，提取它
    if start_keyword in full_line:
        after_keyword = full_line.split(start_keyword, 1)[1].strip()
        if after_keyword and after_keyword != end_keyword:
            # 处理可能的多个内容 (用\n分隔)
            if '\\n' in after_keyword:
                parts = after_keyword.split('\\n')
                for part in parts:
                    part = part.strip()
                    if part and part != end_keyword:
                        # 移除序号
                        number_pattern = r'^\s*[\[]?\d+[\]?:?.]\s*'
                        content = re.sub(number_pattern, '', part).strip()
                        if content:
                            contents.append(content)
            else:
                content = after_keyword.strip()
                if content and content != end_keyword:
                    contents.append(content)
    
    # 继续从后续行提取 (处理多行的情况)
    start_line = latest_match['line_number'] + 1
    
    for i in range(start_line, len(lines)):
        line = lines[i].strip()
        if '→' in line:
            line = line.split('→', 1)[1].strip()
        
        # 检查是否遇到 END
        if end_keyword in line:
            print(f"[信息] 遇到结束关键词 {end_keyword},停止提取")
            break
        
        # 跳过空行
        if not line:
            continue
        
        # 移除序号部分（如果有）
        number_pattern = r'^\s*[\[]?\d+[\]?:?.]\s*'
        content = re.sub(number_pattern, '', line).strip()
        
        if content and content not in contents:
            contents.append(content)
    
    print(f"[成功] 从日志中提取到 {len(contents)} 条内容")
    
    return contents if contents else None


def extract_titles_from_log(log_file: Path) -> list[str] | None:
    """
    从 info.log 文件中提取标题信息（Title.txt: 到 END）
    
    Args:
        log_file: info.log 文件路径
        
    Returns:
        标题列表，如果未找到则返回 None
    """
    return extract_content_from_log(log_file, "Title.txt:", "END")


def read_file_contents(file_path: Path) -> list[str]:
    """
    从文件中读取内容列表
    
    Args:
        file_path: 文件路径
        
    Returns:
        内容列表
    """
    if not file_path.exists():
        return []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 关键修复：处理转义的 \n 字符
    # LLM 输出的可能是字面量 \n 而非真正换行
    if '\\n' in content:
        content = content.replace('\\n', '\n')
    
    # 按行分割
    lines = content.split('\n')
    
    # 清理每行的空白字符，过滤空行和 END 标记
    contents = []
    for line in lines:
        line = line.strip()
        if line and line != 'END':
            # 移除序号（如 "1. " 或 "[1]:"）
            number_pattern = r'^\s*[\[]?\d+[\]?:?.]\s*'
            clean_line = re.sub(number_pattern, '', line).strip()
            if clean_line:
                contents.append(clean_line)
    
    return contents


def rename_images(
    download_dir: str | None = None,
    titles_file: str | None = None,
    log_file: str | None = None,
    record_file: str | None = None,
):
    """
    执行图片重命名
    """
    download_path = Path(download_dir) if download_dir else BASE_DIR / "image"
    titles_path = Path(titles_file) if titles_file else BASE_DIR / "browseruse_agent_data" / "title.txt"
    log_path = Path(log_file) if log_file else BASE_DIR / "info.log"
    agent_data_dir = titles_path.parent
    records_path = Path(record_file) if record_file else find_structured_record_file(agent_data_dir)
    
    print("=" * 60)
    print("[工具] 图片重命名工具")
    print("=" * 60)
    
    # 验证目录存在
    if not download_path.exists():
        print(f"[错误] 目录不存在：{download_path}")
        return False

    # 1. 优先使用结构化下载记录精确重命名，避免 title.txt 顺序与实际文件错位。
    used_record_mode, record_rename_map = rename_images_from_records(download_path, records_path)
    if record_rename_map:
        record_file_path = download_path / 'rename_record.txt'
        with open(record_file_path, 'w', encoding='utf-8') as f:
            f.write(f"图片重命名记录（{records_path.name} 驱动）\n")
            f.write("=" * 60 + "\n\n")
            for record in record_rename_map:
                f.write(f"[{record['index']}] {record['old_name']} → {record['new_name']}\n")
                f.write(f"    状态：{record.get('status', '')}\n")
                f.write(f"    标题：{record['title']}\n")
                if record.get('page_url'):
                    f.write(f"    页面：{record['page_url']}\n")
                if record.get('error'):
                    f.write(f"    错误：{record['error']}\n")
                f.write("\n")
        print(f"[信息] 重命名记录已保存到：{record_file_path}")
        return used_record_mode
    
    # 2. 兼容旧流程：没有结构化记录时，才从 title.txt 顺序重命名。
    titles = read_file_contents(titles_path)
    if not titles:
        print(f"\n[警告] 标题文件为空或不存在：{titles_path}")
        titles = []
    else:
        print(f"\n[成功] 从 title.txt 读取到 {len(titles)} 个标题")
    
    # 7. 获取图片文件
    image_files = get_image_files(download_path)
    print(f"\n[成功] 找到 {len(image_files)} 个图片文件")
    
    if not image_files:
        print("[警告] 目录中没有图片文件")
        return False
    
    # 8. 检查数量是否匹配
    if not titles:
        print("[错误] 未找到标题信息，无法执行重命名")
        return False
    
    if len(titles) != len(image_files):
        print(f"[警告] 警告：标题数量 ({len(titles)}) 与图片数量 ({len(image_files)}) 不匹配")
        if len(titles) < len(image_files):
            print(f"   只有前 {len(titles)} 个图片会被重命名")
        else:
            print(f"   只有前 {len(image_files)} 个标题会被使用")
    
    # 9. 执行重命名
    success_count = 0
    failed_count = 0
    rename_map = []  # 记录重命名映射
    
    for idx, (img_file, title) in enumerate(zip(image_files, titles)):
        old_name = img_file.name
        sanitized_title = sanitize_filename(title)
        
        # 生成新文件名（保留原扩展名）
        new_name = f"{sanitized_title}{img_file.suffix}"
        new_path = img_file.parent / new_name
        
        # 避免文件名冲突
        counter = 1
        while new_path.exists():
            new_name = f"{sanitized_title}_{counter}{img_file.suffix}"
            new_path = img_file.parent / new_name
            counter += 1
        
        try:
            img_file.rename(new_path)
            print(f"[成功] [{idx + 1}/{len(image_files)}] {old_name} → {new_name}")
            success_count += 1
            rename_map.append({
                'index': idx + 1,
                'old_name': old_name,
                'new_name': new_name,
                'title': title
            })
        except Exception as e:
            print(f"[错误] [{idx + 1}/{len(image_files)}] 重命名失败：{old_name} - {e}")
            failed_count += 1
    
    # 10. 输出统计
    print("\n" + "=" * 60)
    print("[统计] 重命名完成")
    print(f"[成功] 成功：{success_count} 个")
    print(f"[错误] 失败：{failed_count} 个")
    print("=" * 60)
    
    # 11. 保存重命名记录
    legacy_record_file = download_path / 'rename_record.txt'
    with open(legacy_record_file, 'w', encoding='utf-8') as f:
        f.write("图片重命名记录\n")
        f.write("=" * 60 + "\n\n")
        for record in rename_map:
            f.write(f"[{record['index']}] {record['old_name']} → {record['new_name']}\n")
            f.write(f"    标题：{record['title']}\n\n")
    
    print(f"[信息] 重命名记录已保存到：{legacy_record_file}")
    
    return success_count > 0


if __name__ == "__main__":
    # 使用基于脚本位置的相对路径
    success = rename_images()
    
    if success:
        print("\n[成功] 重命名任务完成！")
    else:
        print("\n[错误] 重命名任务失败或无需执行")
