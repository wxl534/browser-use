import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from browser_use import Agent, Browser, ChatOpenAI
from idp_page_progress import select_next_page

# 从独立的工具注册模块导入
from tools_registry import (
    DownloadCurrentIdpSearchPageImagesParams,
    NavigateIdpSearchPageParams,
    RebuildLocDownloadStateParams,
    configure_runtime_paths,
    download_current_idp_search_page_images,
    format_download_validation_report,
    navigate_idp_search_page,
    rebuild_loc_download_state,
    tools,
    validate_download_artifacts,
)

# 项目根目录（基于脚本位置，不再硬编码）
BASE_DIR = Path(__file__).resolve().parent

# 全局标志：用于控制是否退出
should_quit = False

IMAGE_EXTENSIONS = ('*.tif', '*.tiff', '*.png', '*.jpg', '*.jpeg', '*.gif', '*.webp')
LOG_ROTATE_THRESHOLD_BYTES = 50 * 1024 * 1024
CACHE_BASE_NAME = 'ImagesCache'


def rotate_large_logs(base_dir: Path, threshold_bytes: int = LOG_ROTATE_THRESHOLD_BYTES) -> Path | None:
    """
    启动前轮转过大的日志文件，避免 debug.log/info.log 无限增长拖慢搜索和排查。
    """
    log_files = [base_dir / 'info.log', base_dir / 'debug.log']
    large_logs = [path for path in log_files if path.exists() and path.stat().st_size >= threshold_bytes]
    if not large_logs:
        return None

    backup_dir = base_dir / 'history' / f'log_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    backup_dir.mkdir(parents=True, exist_ok=True)
    for log_file in large_logs:
        target = backup_dir / log_file.name
        try:
            shutil.move(str(log_file), str(target))
            log_file.write_text('', encoding='utf-8')
        except PermissionError:
            shutil.copy2(log_file, target)
            try:
                log_file.write_text('', encoding='utf-8')
            except PermissionError:
                print(f"⚠️ 日志文件正在被占用，已复制备份但无法清空：{log_file}")
    print(f"🧹 已轮转过大日志到：{backup_dir}")
    return backup_dir

def monitor_input_windows():
    """
    Windows 平台的后台线程：监听键盘输入，如果输入 'quit' 则设置退出标志
    """
    global should_quit
    import msvcrt
    
    print("\n💡 提示：在运行过程中输入 'quit' 可以停止程序运行")
    print("=" * 60 + "\n")
    
    input_buffer = []
    
    try:
        while not should_quit:
            # 非阻塞检查键盘输入
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                
                # 处理回车键
                if char == '\r' or char == '\n':
                    command = ''.join(input_buffer).strip().lower()
                    input_buffer = []
                    
                    if command == 'quit':
                        print("\n\n⚠️  收到退出指令，正在停止运行...")
                        should_quit = True
                        break
                    elif command:  # 忽略空输入
                        print(f"\n⚠️  未知命令: {command}，请输入 'quit' 停止运行")
                elif char == '\b' or char == '\x08':  # 退格键
                    if input_buffer:
                        input_buffer.pop()
                        print('\b \b', end='', flush=True)  # 删除屏幕上的字符
                elif char == '\x03':  # Ctrl+C
                    should_quit = True
                    break
                else:
                    input_buffer.append(char)
                    print(char, end='', flush=True)  # 显示输入的字符
            
            # 短暂休眠避免占用过多 CPU
            time.sleep(0.01)
    except KeyboardInterrupt:
        should_quit = True
    except Exception as e:
        print(f"\n⚠️  输入监听异常: {e}")
        should_quit = True

def monitor_input_default():
    """
    非 Windows 平台的后台线程：监听终端输入
    """
    global should_quit
    
    print("\n💡 提示：在运行过程中输入 'quit' 可以停止程序运行")
    print("=" * 60 + "\n")
    
    try:
        while not should_quit:
            try:
                line = sys.stdin.readline()
                if line:
                    command = line.strip().lower()
                    if command == 'quit':
                        print("\n\n⚠️  收到退出指令，正在停止运行...")
                        should_quit = True
                        break
            except Exception:
                pass
            time.sleep(0.1)
    except KeyboardInterrupt:
        should_quit = True

def start_input_monitor():
    """
    启动输入监听线程（自动选择适合当前平台的实现）
    """
    if os.name == 'nt':  # Windows
        monitor_thread = threading.Thread(target=monitor_input_windows, daemon=True)
    else:  # Linux/Mac
        monitor_thread = threading.Thread(target=monitor_input_default, daemon=True)
    
    monitor_thread.start()
    return monitor_thread


def extract_target_image_count(task: str, default: int = 1) -> int:
    """
    从 task.md 中提取目标图片数量，默认读取 `n = <number>`。
    """
    match = re.search(r'\bn\s*=\s*(\d+)\b', task, re.IGNORECASE)
    if not match:
        return default
    return max(1, int(match.group(1)))


def extract_search_keyword(task: str, default: str = 'china buddhist') -> str:
    """
    从 task.md 中提取 IDP 搜索关键词，避免续跑上下文硬编码旧关键词。
    """
    patterns = [
        r'搜索关键词\s*\*\*`([^`]+)`\*\*',
        r'搜索关键词固定为\s*`([^`]+)`',
        r'关键词\s*`([^`]+)`',
    ]
    for pattern in patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match and match.group(1).strip():
            return re.sub(r'\s+', ' ', match.group(1)).strip()
    return default


def keyword_title_prefix(keyword: str) -> str:
    return re.sub(r'[^0-9A-Za-z]+', '_', keyword.lower()).strip('_') or 'idp_image'


def sanitize_run_folder_name(keyword: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', keyword_title_prefix(keyword))
    name = re.sub(r'_+', '_', name).strip('._ ')[:120]
    return name or 'idp_run'


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cache_is_locked(cache_dir: Path) -> bool:
    lock_file = cache_dir / 'run.lock'
    if not lock_file.exists():
        return False
    try:
        lock = json.loads(lock_file.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return False
    try:
        pid = int(lock.get('pid') or 0)
    except (TypeError, ValueError):
        return False
    return process_is_running(pid)


def cache_has_content(cache_dir: Path) -> bool:
    return cache_dir.exists() and any(cache_dir.iterdir())


def archive_cache(images_root: Path, cache_dir: Path, keyword: str) -> Path | None:
    if not cache_has_content(cache_dir):
        return None
    archive_name = sanitize_run_folder_name(keyword)
    target_dir = images_root / archive_name
    if target_dir.exists():
        target_dir = images_root / f'{archive_name}_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    shutil.move(str(cache_dir), str(target_dir))
    print(f"🗃️ 已归档上一次运行缓存：{cache_dir} -> {target_dir}")
    return target_dir


def select_active_cache_dir(base_dir: Path, *, resume_run: bool, keyword: str) -> Path:
    """
    选择本次运行的 ImagesCache。新流程会先把旧 ImagesCache 归档为搜索词目录。
    如果已有运行中的 lock，则使用 ImagesCache_01 / _02。
    """
    images_root = base_dir / 'Images'
    images_root.mkdir(parents=True, exist_ok=True)
    base_cache = images_root / CACHE_BASE_NAME

    if not resume_run and base_cache.exists() and not cache_is_locked(base_cache):
        archive_cache(images_root, base_cache, keyword)

    candidates = [base_cache, *[images_root / f'{CACHE_BASE_NAME}_{index:02d}' for index in range(1, 100)]]
    if resume_run:
        for candidate in candidates:
            if candidate.exists() and not cache_is_locked(candidate):
                return candidate

    for candidate in candidates:
        if not cache_is_locked(candidate):
            if not resume_run and cache_has_content(candidate):
                archive_cache(images_root, candidate, keyword)
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
    raise RuntimeError('没有可用的 ImagesCache 目录；请检查并关闭其他运行中的任务。')


def write_run_lock(run_dir: Path, keyword: str, target_image_count: int, resume_run: bool) -> None:
    lock = {
        'pid': os.getpid(),
        'keyword': keyword,
        'target_count': target_image_count,
        'resume': resume_run,
        'started_at': datetime.now().isoformat(),
        'run_dir': str(run_dir),
    }
    (run_dir / 'run.lock').write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir / 'run_config.json').write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding='utf-8')


def is_loc_download_task(task: str) -> bool:
    """
    判断当前任务是否依赖 LOC 专用队列/下载记录。
    非 LOC 任务不能运行 LOC 状态重建，否则会覆盖通用 title.txt。
    """
    loc_markers = (
        'loc.gov',
        'www.loc.gov',
        'Library of Congress',
        '美国国会图书馆',
    )
    return any(marker in task for marker in loc_markers)


def read_task_file(task_file: Path) -> str:
    """
    同步读取 task 文件内容，避免在 async main 中直接做阻塞文件 I/O。
    """
    return task_file.read_text(encoding='utf-8').strip()


def build_agent_run_limits(target_image_count: int) -> tuple[int, int, int]:
    """
    根据目标下载数量设置运行限制。下载任务主要靠批量工具完成，
    不应允许 LLM 超时/浏览器异常累计到上千次才停止。
    """
    safe_target = max(1, target_image_count)
    max_failures = min(80, max(20, safe_target // 20))
    max_actions_per_step = 4 if safe_target >= 25 else 3
    max_steps = max(1000, safe_target * 20)
    return max_failures, max_actions_per_step, max_steps


def backup_runtime_state(run_dir: Path, files: list[Path]) -> Path | None:
    """
    清理运行状态前先完整备份 browseruse_agent_data，并补充备份旧版 image/rename_record.txt。
    """
    existing_files = [path for path in files if path.exists() and path.is_file()]
    if not cache_has_content(run_dir) and not existing_files:
        return None

    backup_dir = run_dir.parent / f'{run_dir.name}_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    backup_dir.mkdir(parents=True, exist_ok=True)
    if cache_has_content(run_dir):
        shutil.copytree(run_dir, backup_dir / run_dir.name, dirs_exist_ok=True)
    for path in existing_files:
        relative_path = path.relative_to(run_dir)
        target_path = backup_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target_path)
    return backup_dir


def prepare_runtime_state(run_dir: Path, reset_state: bool = True) -> tuple[Path, Path, Path]:
    """
    初始化本次运行需要的目录和状态文件，避免旧结果污染长任务。
    """
    image_dir = run_dir
    agent_data_dir = run_dir
    title_file = run_dir / 'title.txt'
    rename_record_file = run_dir / 'rename_record.txt'
    image_record_file = run_dir / 'image_record.jsonl'
    temple_info_file = run_dir / 'temple_photo_info.md'
    kyohaku_strategy_file = run_dir / 'kyohaku_download_strategy.json'
    idp_progress_file = run_dir / 'idp_progress.json'

    run_dir.mkdir(parents=True, exist_ok=True)

    if reset_state:
        backup_dir = backup_runtime_state(
            run_dir,
            [title_file, rename_record_file, image_record_file, temple_info_file, kyohaku_strategy_file, idp_progress_file],
        )
        if backup_dir:
            print(f"🛟 已完整备份旧 ImagesCache 到：{backup_dir}")

    if reset_state and rename_record_file.exists():
        rename_record_file.unlink()

    if reset_state:
        for path in list(run_dir.iterdir()):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    return image_dir, agent_data_dir, title_file


def count_titles(title_file: Path) -> int:
    """
    统计 title.txt 中的有效标题数量（排除空行和 END）。
    """
    if not title_file.exists():
        return 0

    with open(title_file, 'r', encoding='utf-8') as f:
        return len([line for line in f if line.strip() and line.strip().upper() != 'END'])


def count_downloaded_records(record_file: Path) -> int:
    """
    统计 download_record.jsonl 中 status=downloaded 的成功记录数量。
    """
    if not record_file.exists():
        return 0

    count = 0
    for line in record_file.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get('status') == 'downloaded':
            count += 1
    return count


def count_existing_image_files(image_dir: Path) -> int:
    """
    统计 image 目录中的本地图片文件数量，用于判断是否更适合默认续跑。
    """
    if not image_dir.exists():
        return 0
    total = 0
    for pattern in IMAGE_EXTENSIONS:
        total += len([path for path in image_dir.glob(pattern) if path.is_file()])
    return total


def ask_resume_from_checkpoint(base_dir: Path) -> bool:
    """
    启动时询问是否保留上次记录并从断点续跑。
    未设置 BROWSER_USE_RESUME_RUN 时必须明确输入 y 或 n，避免误按回车走错流程。
    """
    env_value = os.environ.get('BROWSER_USE_RESUME_RUN', '').strip().lower()
    if env_value in {'1', 'true', 'yes', 'on'}:
        print("♻️  BROWSER_USE_RESUME_RUN=1：自动选择从上次断点继续")
        return True
    if env_value in {'0', 'false', 'no', 'off'}:
        print("🆕 BROWSER_USE_RESUME_RUN=0：自动选择开启新流程")
        return False

    cache_dir = base_dir / 'Images' / CACHE_BASE_NAME
    record_file = cache_dir / 'image_record.jsonl'
    image_dir = cache_dir
    if not record_file.exists():
        record_file = base_dir / 'browseruse_agent_data' / 'image_record.jsonl'
        image_dir = base_dir / 'image'
    existing_count = 0
    if record_file.exists():
        existing_count = count_downloaded_records(record_file)
        image_count = count_existing_image_files(image_dir)
        print(f"\n📌 检测到上次运行状态：{record_file}（downloaded={existing_count}，本地图片={image_count}）")
    else:
        image_count = count_existing_image_files(image_dir)
        if image_count:
            print(f"\n📌 未检测到上次图片记录，但 image 目录已有 {image_count} 个图片文件")
        else:
            print("\n📌 未检测到上次图片记录")

    while True:
        try:
            answer = input("是否接着上一次的断点运行？输入 y/yes 继续，输入 n/no 开启新流程 [y/n]: ")
        except (EOFError, KeyboardInterrupt):
            print("\n🛑 未收到明确选择，为避免误清理或误续跑，已停止启动")
            raise SystemExit(130)

        normalized_answer = answer.strip().lower()
        if normalized_answer in {'y', 'yes', '是', '继续', 'resume'}:
            return True
        if normalized_answer in {'n', 'no', '否', '新流程', 'new'}:
            return False
        print("⚠️ 请输入 y/yes 继续上次断点，或 n/no 开启新流程。")


def load_downloaded_image_records(record_file: Path) -> list[dict]:
    """
    读取通用图片记录，只保留 status=downloaded 的有效记录。
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
        if isinstance(record, dict) and record.get('status') == 'downloaded':
            records.append(record)

    def sort_key(record: dict) -> tuple[int, str]:
        try:
            sequence = int(record.get('sequence') or 0)
        except (TypeError, ValueError):
            sequence = 0
        return sequence, str(record.get('file_name') or '')

    return sorted(records, key=sort_key)


def load_idp_progress(base_dir: Path) -> dict:
    progress_file = base_dir / 'idp_progress.json'
    if not progress_file.exists():
        progress_file = base_dir / 'browseruse_agent_data' / 'idp_progress.json'
    if not progress_file.exists():
        return {}
    try:
        data = json.loads(progress_file.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def sync_idp_progress_from_page_queue(run_dir: Path, target_image_count: int, search_keyword: str) -> dict:
    """
    使用 idp_page_progress.json 选择续跑页，并同步导出到 idp_progress.json。
    image_record.jsonl 仍是图片级事实来源；idp_page_progress.json 是页级事实来源。
    """
    legacy_progress = load_idp_progress(run_dir)
    try:
        fallback_page = max(1, int(legacy_progress.get('next_page') or legacy_progress.get('current_page') or 1))
    except (TypeError, ValueError):
        fallback_page = 1
    active = select_next_page(
        run_dir,
        keyword=search_keyword,
        target_count=target_image_count,
        fallback_page=fallback_page,
        max_reasonable_page=max(200, (target_image_count // 25) + 20),
    )
    progress = {
        **legacy_progress,
        'keyword': search_keyword,
        'target_count': target_image_count,
        'current_page': active['page'],
        'next_page': active['page'],
        'next_index': active['next_index'],
        'source': 'synced_from_idp_page_progress',
        'page_progress_file': str(run_dir / 'idp_page_progress.json'),
        'updated_at': datetime.now().isoformat(),
    }
    (run_dir / 'idp_progress.json').write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
    return active


def build_resume_task_context(base_dir: Path, target_image_count: int, search_keyword: str = 'china buddhist') -> str:
    """
    根据 browseruse_agent_data/image_record.jsonl 生成断点续跑说明，附加到 task 给 agent。
    """
    record_file = base_dir / 'image_record.jsonl'
    if not record_file.exists():
        record_file = base_dir / 'browseruse_agent_data' / 'image_record.jsonl'
    records = load_downloaded_image_records(record_file)
    if not records:
        return (
            "\n\n## 断点续跑上下文\n\n"
            "用户选择了从断点继续，但 `browseruse_agent_data/image_record.jsonl` 中没有可用的 downloaded 记录。"
            "请从第 1 张开始执行；仍然使用工具的安全序号，不能覆盖已有文件。\n"
        )

    def sequence_of(record: dict) -> int:
        try:
            return int(record.get('sequence') or 0)
        except (TypeError, ValueError):
            return 0

    max_sequence = max(sequence_of(record) for record in records)
    next_sequence = max_sequence + 1
    remaining_by_count = max(0, target_image_count - len(records))
    image_count = count_existing_image_files(base_dir)
    if image_count == 0:
        image_count = count_existing_image_files(base_dir / 'image')
    progress = load_idp_progress(base_dir)
    progress_page = progress.get('next_page') or progress.get('current_page')
    progress_index = progress.get('next_index', 0)
    try:
        suggested_page = max(1, int(progress_page))
    except (TypeError, ValueError):
        suggested_page = max(1, (max_sequence // 50) + 1)
    try:
        suggested_start_index = max(0, int(progress_index))
    except (TypeError, ValueError):
        suggested_start_index = 0
    title_prefix = keyword_title_prefix(search_keyword)
    last_record = records[-1]
    listed_records = records[-120:]
    listed_lines = [
        (
            f"- #{sequence_of(record):03d} | {record.get('file_name', '')} | "
            f"{record.get('collection_title') or record.get('title') or 'untitled'} | "
            f"{record.get('page_url', '')} | {record.get('image_url', '')}"
        )
        for record in listed_records
    ]

    return (
        "\n\n## 断点续跑上下文\n\n"
        "用户选择了接着上一次断点运行。你必须把下面的记录视为已经成功下载，不能重新下载或重新记录。\n\n"
        f"- 记录文件：`{record_file}`\n"
        f"- 已成功记录图片数：{len(records)}\n"
        f"- 本地 image 目录图片数：{image_count}\n"
        f"- IDP 进度文件：`{base_dir / 'idp_progress.json'}`\n"
        f"- 建议续跑搜索页：page={suggested_page}, start_index={suggested_start_index}\n"
        f"- 已使用最大序号：{max_sequence}\n"
        f"- 下一张新图片必须从序号：{next_sequence} 开始，例如 `temple_{next_sequence:03d}` / "
        f"`{title_prefix}_{next_sequence:03d}_...`\n"
        f"- 程序已在 Agent 启动前按页级进度预执行第一批动作；Agent 接手后必须从最新 `idp_progress.json` 继续。\n"
        f"- 恢复后不要从搜索结果第 1 页重新扫描；如需继续批量处理，使用 "
        f"`navigate_idp_search_page(keyword=\"{search_keyword}\", page={suggested_page}, limit=50)` "
        f"和 `download_current_idp_search_page_images(..., start_index={suggested_start_index}, ...)`。\n"
        f"- 必须按 `idp_page_progress.json` 同步导出的 page={suggested_page} 顺序递增；不要自行跳到 page 999、page 5000 或其他极深页。\n"
        "- 如果当前页批量工具 0 新增或失败，改为下一页继续批量处理；不要退化为逐个点击详情页。\n"
        f"- 按有效记录数距离目标 {target_image_count} 还需要继续处理：{remaining_by_count} 张；"
        f"不要因为最大序号达到目标就提前结束，也不要回头补齐旧序号空洞\n"
        f"- 上一次最后记录：#{sequence_of(last_record):03d}，文件 `{last_record.get('file_name', '')}`，"
        f"标题 `{last_record.get('collection_title') or last_record.get('title') or ''}`，"
        f"页面 `{last_record.get('page_url', '')}`\n\n"
        "续跑规则：\n"
        "1. 不要清空 image 目录、title.txt、image_record.jsonl 或 temple_photo_info.md。\n"
        "2. 必须把下面列出的详情页 URL、manifest URL、图片 URL 都视为已处理；搜索结果中如果再次遇到这些记录，直接跳过，不要调用下载。\n"
        "3. 如果误点到已处理详情页，调用工具后返回“图片 URL 已有下载记录”或“详情页已处理”时，视为成功跳过并继续下一条。\n"
        f"4. 第一次保存新图片时传入 sequence={next_sequence}，之后按安全序号递增；如果工具自动修正序号，以工具返回为准；目标以有效记录数量为准，不以文件夹文件数或最大序号为准。\n\n"
        f"已下载记录清单（共列出 {len(listed_records)} 条，用于避开重复）：\n"
        + "\n".join(listed_lines)
        + "\n"
    )


async def run_idp_resume_preflight(
    *,
    browser: Browser,
    run_dir: Path,
    target_image_count: int,
    search_keyword: str,
) -> None:
    """
    断点续跑时由代码先完成第一组确定性动作，避免依赖 Agent 记住 prompt。
    """
    active = sync_idp_progress_from_page_queue(run_dir, target_image_count, search_keyword)
    page = int(active.get('page') or 1)
    start_index = int(active.get('next_index') or 0)
    print(f"\n♻️ 代码预执行断点首批动作：page={page}, start_index={start_index}")

    navigate_result = await navigate_idp_search_page(
        params=NavigateIdpSearchPageParams(keyword=search_keyword, page=page, limit=50),
        browser_session=browser,
    )
    if navigate_result.error:
        print(f"⚠️ 断点预导航失败，交给 Agent 处理：{navigate_result.error}")
        return
    if navigate_result.extracted_content:
        print(navigate_result.extracted_content)

    batch_result = await download_current_idp_search_page_images(
        params=DownloadCurrentIdpSearchPageImagesParams(
            target_count=target_image_count,
            max_items=50,
            start_index=start_index,
            images_per_item=1,
            file_prefix='temple',
            title_prefix=keyword_title_prefix(search_keyword),
            allowed_host_suffixes=['idp.bl.uk', 'data.idp.bl.uk', 'bl.uk'],
            record_filename='image_record.jsonl',
            info_filename='temple_photo_info.md',
        ),
        browser_session=browser,
    )
    if batch_result.error:
        print(f"⚠️ 断点预批量下载失败，交给 Agent 处理：{batch_result.error}")
        return
    if batch_result.extracted_content:
        print(batch_result.extracted_content)


async def rebuild_download_state_for_run(rewrite_title_file: bool) -> None:
    """
    在运行前后重建队列/标题状态，清掉中断留下的 in_progress。
    """
    result = await rebuild_loc_download_state(params=RebuildLocDownloadStateParams(
        remove_irrelevant=True,
        reset_in_progress=True,
        rewrite_title_file=rewrite_title_file,
    ))
    if result.error:
        print(f"⚠️ LOC 状态重建失败：{result.error}")
    elif result.extracted_content:
        print(result.extracted_content)


def ensure_title_end_marker(title_file: Path) -> bool:
    """
    如果 title.txt 已经有标题但没有 END，补写 END 标记。
    """
    if not title_file.exists():
        return False

    lines = title_file.read_text(encoding='utf-8').splitlines()
    meaningful_lines = [line.strip() for line in lines if line.strip()]
    if not meaningful_lines:
        return False

    if meaningful_lines[-1].upper() == 'END':
        return False

    with open(title_file, 'a', encoding='utf-8') as f:
        if not title_file.read_text(encoding='utf-8').endswith('\n'):
            f.write('\n')
        f.write('END\n')

    return True


async def finalize_download_run(
    history,
    *,
    should_quit: bool,
    loc_download_task: bool,
    title_file: Path,
    image_dir: Path,
    agent_data_dir: Path,
    target_image_count: int,
) -> None:
    """
    无论正常结束还是用户手动停止，都执行状态重建、验证和自动重命名。
    """
    if loc_download_task:
        print("\n🧹 运行后重建 LOC 下载状态，按 download_record.jsonl 同步 title.txt")
        await rebuild_download_state_for_run(rewrite_title_file=True)
    else:
        print("\nℹ️ 当前不是 LOC 下载任务，保留通用 title.txt / image_record.jsonl")

    if ensure_title_end_marker(title_file):
        print(f"✅ 已为标题文件补写 END 标记：{title_file}")

    if should_quit:
        print("\n🛑 程序已被用户手动停止，继续执行收尾验证和重命名")

    print("\n=== 下载结果验证 ===")
    all_image_files = []
    if image_dir.exists():
        found = []
        for ext in IMAGE_EXTENSIONS:
            found.extend(image_dir.glob(ext))
        all_image_files.extend(found)
        print(f"✓ 目录 {image_dir} 中找到 {len(found)} 个图片文件")
    else:
        print(f"ℹ️ 目录不存在：{image_dir}")

    title_count = count_titles(title_file)
    structured_record_file = agent_data_dir / ('download_record.jsonl' if loc_download_task else 'image_record.jsonl')
    downloaded_record_count = count_downloaded_records(structured_record_file)
    print(
        f"\n📊 结果汇总：目标 {target_image_count} 张，"
        f"已记录标题 {title_count} 个，结构化记录 {downloaded_record_count} 条，已下载图片 {len(all_image_files)} 个"
    )

    if all_image_files:
        print(f"\n总共找到 {len(all_image_files)} 个下载的文件:")
        for img_file in sorted(all_image_files):
            file_size = img_file.stat().st_size
            print(f"  - {img_file.name}: {file_size:,} 字节")
            if file_size == 0:
                print(f"  ⚠️ 警告：{img_file.name} 文件大小为 0")
    else:
        print("❌ 未找到任何下载的文件")
        if title_file.exists():
            print(f"✓ title.txt 存在，包含 {title_count} 个标题")
        else:
            print("⚠️ title.txt 不存在")

    errors = history.errors()
    if any(errors):
        error_count = sum(1 for e in errors if e is not None)
        print(f"\n⚠️ 执行过程中出现 {error_count} 个错误")

    failed_record_file = agent_data_dir / 'kyohaku_failed_record.jsonl'
    failed_count = 0
    if failed_record_file.exists():
        failed_count = len([line for line in failed_record_file.read_text(encoding='utf-8').splitlines() if line.strip()])
    if failed_count:
        print(f"\n⚠️ Kyohaku 持久化失败记录：{failed_count} 条，文件 {failed_record_file}")

    print("\n=== 任务统计 ===")
    print(f"总步数：{history.number_of_steps()}")
    print(f"总耗时：{history.total_duration_seconds():.2f} 秒")
    print(f"访问 URL 数：{len(history.urls())}")

    def write_final_validation() -> dict:
        validation_result = validate_download_artifacts(target_count=target_image_count)
        report = format_download_validation_report(validation_result)
        report_file = agent_data_dir / 'final_download_report.md'
        report_file.write_text(report + '\n', encoding='utf-8')
        print("\n=== 最终下载校验（以本地记录为准） ===")
        print(report)
        if validation_result['complete']:
            print("✅ 最终校验通过：可以视为任务成功")
        else:
            print("❌ 最终校验未通过：不能声称已完成目标数量")

        if not loc_download_task:
            print("\n=== SQLite 数据库导入 ===")
            sqlite_import_script = BASE_DIR / 'import_records_to_sqlite.py'
            db_file = agent_data_dir / 'image_catalog.sqlite3'
            import_ok = run_python_script(
                str(sqlite_import_script),
                "SQLite 图片记录导入",
                [
                    '--record-file',
                    str(agent_data_dir / 'image_record.jsonl'),
                    '--image-dir',
                    str(image_dir),
                    '--db-file',
                    str(db_file),
                ],
            )
            if import_ok:
                print(f"✅ SQLite 数据库已更新：{db_file}")
            else:
                print(f"⚠️ SQLite 数据库导入失败，请手动执行：python {sqlite_import_script}")
        return validation_result

    if not all_image_files:
        print("💡 未检测到下载图片，跳过自动重命名")
        write_final_validation()
        return

    if title_count == 0 and downloaded_record_count == 0:
        print("💡 未收集到标题或下载记录，跳过自动重命名")
        write_final_validation()
        return

    print("\n✅ 图片下载工具已在每张图片保存成功后立即完成最终命名，跳过旧的批量重命名步骤")
    write_final_validation()

# 临时解决方案：绑定 hosts
# 10.64.84.182 openapi.seu.edu.cn
load_dotenv()

# === 完全禁用截图功能的环境变量配置 ===
# 增加点击事件超时时间，避免下载等待时的超时警告
os.environ['TIMEOUT_ClickElementEvent'] = '60.0'  # 从默认 15s 增加到 60s
os.environ['TIMEOUT_ScreenshotEvent'] = '60.0'    # 截图事件超时也增加
os.environ.setdefault('BROWSER_USE_LIGHTWEIGHT_DOWNLOADS', '1')
os.environ.setdefault('BROWSER_USE_DISABLE_SCREENSHOTS', '1')
os.environ.setdefault('BROWSER_USE_LIGHTWEIGHT_DOM', '1')
print("✅ 已配置环境变量：启用轻量下载/DOM模式，禁用截图，增加事件超时时间")

#临时添加 host 映射,仅用在学校llm
def add_host_mapping(host, ip):
    """临时添加 host 映射到本地"""
    try:
        # 尝试解析域名，看是否已经配置
        socket.gethostbyname(host)
        print(f"✓ Host '{host}' 已配置")
    except socket.gaierror:
        print(f"⚠ 注意：需要在系统 hosts 文件中添加映射：{ip} {host}")
        print("  Windows: C:\\Windows\\System32\\drivers\\etc\\hosts")
        print(f"  以管理员身份运行记事本，添加：{ip} {host}")

# 检查 host 配置
add_host_mapping('openapi.seu.edu.cn', '10.64.84.182')

# === 导入工具函数 ===
def run_python_script(
    script_path: str,
    description: str = "脚本",
    extra_args: list[str] | None = None,
    timeout_seconds: int = 600,
) -> bool:
    """
    运行 Python 脚本的辅助函数
    
    Args:
        script_path: 脚本的绝对路径
        description: 脚本描述
        extra_args: 额外的命令行参数
        
    Returns:
        是否成功执行
    """
    script = Path(script_path)
    
    if not script.exists():
        print(f"⚠️ 警告:{description}脚本不存在:{script}")
        return False
    
    try:
        cmd = [sys.executable, str(script)]
        if extra_args:
            cmd.extend(extra_args)
        # 使用当前 Python 解释器运行(避免环境问题)
        # 先以二进制模式捕获输出,避免编码错误
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=str(script.parent),  # 使用脚本所在目录作为工作目录
            env={**os.environ.copy(), 'PYTHONIOENCODING': 'utf-8'}  # 继承当前环境变量并强制 UTF-8 输出
        )
        
        # 手动解码输出:优先尝试 UTF-8,失败则用 GBK(Windows 中文环境)
        try:
            stdout = result.stdout.decode('utf-8')
        except UnicodeDecodeError:
            try:
                stdout = result.stdout.decode('gbk', errors='replace')
            except Exception:
                stdout = result.stdout.decode('utf-8', errors='replace')
        
        try:
            stderr = result.stderr.decode('utf-8')
        except UnicodeDecodeError:
            try:
                stderr = result.stderr.decode('gbk', errors='replace')
            except Exception:
                stderr = result.stderr.decode('utf-8', errors='replace')
        
        # 打印输出
        if stdout:
            print(f"\n📝 {description}输出:")
            print(stdout)
        
        if stderr:
            print(f"\n⚠️ {description}警告/错误:")
            print(stderr)
        
        if result.returncode == 0:
            print(f"\n✅ {description}完成!")
            return True
        else:
            print(f"\n❌ {description}失败,返回码:{result.returncode}")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"\n❌ {description}超时(超过 {timeout_seconds} 秒)")
        return False
    except Exception as e:
        print(f"\n❌ 执行{description}时出错:{e}")
        return False


async def run_agent_once(resume_run_override: bool | None = None):
    global should_quit
    should_quit = False
    rotate_large_logs(BASE_DIR)

    # === 1. 从 task.md 文件读取任务描述 ===
    task_file = BASE_DIR / 'task.md'

    if not task_file.exists():
        print(f"❌ Task 文件不存在：{task_file}")
        raise FileNotFoundError(f"Task file not found: {task_file}")

    print(f"📄 从文件读取 task: {task_file}")
    task = read_task_file(task_file)
    print(f"✅ 成功读取 task，长度：{len(task)} 字符")

    target_image_count = extract_target_image_count(task, default=1)
    search_keyword = extract_search_keyword(task)
    max_failures, max_actions_per_step, max_steps = build_agent_run_limits(target_image_count)
    resume_run = resume_run_override if resume_run_override is not None else ask_resume_from_checkpoint(BASE_DIR)
    loc_download_task = is_loc_download_task(task)
    run_dir = select_active_cache_dir(BASE_DIR, resume_run=resume_run, keyword=search_keyword)
    configure_runtime_paths(run_dir=run_dir, image_dir=run_dir, data_dir=run_dir)
    write_run_lock(run_dir, search_keyword, target_image_count, resume_run)
    print(f"📁 本次运行缓存目录：{run_dir}")
    print(
        f"🎯 本次任务目标：下载前 {target_image_count} 张图片 "
        f"(max_failures={max_failures}, max_actions_per_step={max_actions_per_step}, max_steps={max_steps})"
    )

    if resume_run:
        print("\n♻️  保留现有 ImagesCache，从断点继续")
    else:
        print("\n🆕 已归档旧 ImagesCache（如存在），并创建新的 ImagesCache")
    
    # === 2.5 清空 Information.md（功能已实现，暂时注释） ===
    # 取消下面两行注释即可启用 Information.md 自动清理：
    # from move_images import clear_information_md
    # clear_information_md(interactive=False)
    
    # 等待一下确保文件系统更新完成
    await asyncio.sleep(1)
    
    # === 3. 初始化本次运行状态并创建浏览器与 llm 实例 ===
    image_dir, agent_data_dir, title_file = prepare_runtime_state(run_dir, reset_state=False)
    if resume_run and not loc_download_task:
        active = sync_idp_progress_from_page_queue(run_dir, target_image_count, search_keyword)
        print(f"✅ 已从 idp_page_progress.json 选择续跑页：page={active['page']}, start_index={active['next_index']}")
        resume_context = build_resume_task_context(run_dir, target_image_count, search_keyword)
        task = task + resume_context
        print("✅ 已读取 image_record.jsonl，并把断点续跑上下文加入本次任务")
    if loc_download_task:
        print("\n🧹 启动前重建 LOC 队列状态，重置上次中断留下的 in_progress")
        await rebuild_download_state_for_run(rewrite_title_file=resume_run)
    else:
        print("\nℹ️ 当前不是 LOC 下载任务，跳过 LOC 队列状态重建")

    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    base_url = os.environ.get('OPENAI_BASE_URL', 'https://openapi.seu.edu.cn/v1')

    if not api_key:
        raise ValueError('未设置 OPENAI_API_KEY，无法启动 Agent。请先在 .env 或环境变量中配置。')

    browser = Browser(
        args=[
            f'--user-data-dir={BASE_DIR / "browser_profile"}'
        ],
        headless=False,
        enable_default_extensions=False,
        downloads_path=str(image_dir),  # 下载文件保存到 image 目录
    )

    if resume_run and not loc_download_task:
        await run_idp_resume_preflight(
            browser=browser,
            run_dir=run_dir,
            target_image_count=target_image_count,
            search_keyword=search_keyword,
        )

    llm = ChatOpenAI(
        model='qwen3.5-397b-a17b',
        api_key=api_key,
        base_url=base_url,
        temperature=0.0
    )
    # llm = ChatBrowserUse()  # 官方 LLM，需付费订阅

    # quit 回调：agent 每步执行前会调用此函数，返回 True 则停止
    async def check_should_quit() -> bool:
        return should_quit

    # === 4. 创建 Agent（完全禁用截图，使用 JS 提取） ===
    agent = Agent(
        task=task,  # 使用从.md 文件读取的 task
        llm=llm,
        browser=browser,
        tools=tools,
        use_vision=False,
        max_failures=max_failures,
        max_actions_per_step=max_actions_per_step,
        step_timeout=int(os.environ.get('BROWSER_USE_STEP_TIMEOUT', '240')),
        llm_timeout=int(os.environ.get('BROWSER_USE_LLM_TIMEOUT', '180')),
        register_should_stop_callback=check_should_quit,
        file_system_path=str(BASE_DIR),
        available_file_paths=[
            str(BASE_DIR / 'image'),
            str(run_dir),
            str(agent_data_dir),
            str(BASE_DIR / 'Information.md'),
            str(BASE_DIR / 'source.html'),
            str(title_file),
        ],
    )
    
    # === 5. 运行 agent ===
    print("\n🚀 开始执行任务...")
    
    # 启动输入监听线程；auto_run_until_target.py 会自己监听 quit，避免父子进程抢输入
    input_thread = None
    if os.environ.get('BROWSER_USE_DISABLE_INPUT_MONITOR', '').strip().lower() not in {'1', 'true', 'yes', 'on'}:
        input_thread = start_input_monitor()
    
    try:
        history = await agent.run(max_steps=max_steps)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断执行 (Ctrl+C)")
        return None

    await finalize_download_run(
        history,
        should_quit=should_quit,
        loc_download_task=loc_download_task,
        title_file=title_file,
        image_dir=image_dir,
        agent_data_dir=agent_data_dir,
        target_image_count=target_image_count,
    )

    return history


async def main():
    return await run_agent_once()

if __name__ == "__main__":
    asyncio.run(main())
