"""
Run browser-use repeatedly until ImagesCache reaches the target in task.md.

This supervisor intentionally starts main.py as a fresh subprocess each round.
That gives every retry a clean Python/browser process while preserving checkpoint
state in Images/ImagesCache.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from configure_resume_target import DEFAULT_CACHE_DIR, TASK_FILE, configure_target, detect_task_target
from idp_page_progress import select_next_page


BASE_DIR = Path(__file__).resolve().parent

# main.py 用退出码 3 表示 LLM 端点被网关/门户拦截（致命、不可重试）。
LLM_BLOCKED_EXIT_CODE = 3


def terminate_process_tree(process: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    """终止子进程及其全部后代（如 main.py 启动的 Chromium）。

    仅 process.terminate() 在 Windows 上只杀 main.py，会留下孤儿 Chrome 占住
    browser_profile 锁，导致下一轮起不来。这里优先用 psutil 递归清理，
    失败时回退到 Windows 的 `taskkill /T /F /PID` 或 POSIX 的进程组信号。
    """
    if process.poll() is not None:
        return
    pid = process.pid

    # 首选：psutil 递归终止（browser-use 运行时本就依赖 psutil，跨平台最稳）。
    try:
        import psutil  # type: ignore

        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        procs = parent.children(recursive=True)
        procs.append(parent)
        for proc in procs:
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                pass
        _gone, alive = psutil.wait_procs(procs, timeout=grace_seconds)
        for proc in alive:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        return
    except Exception:
        pass

    # 回退：按平台杀进程树。
    try:
        if os.name == 'nt':
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import signal as _signal

            try:
                os.killpg(os.getpgid(pid), _signal.SIGTERM)
                time.sleep(grace_seconds)
                if process.poll() is None:
                    os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        # 最后兜底：至少杀掉直接子进程。
        try:
            process.kill()
        except Exception:
            pass


def yes_or_no(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in {'y', 'yes', '是'}:
            return True
        if answer in {'n', 'no', '否', ''}:
            return False
        print('请输入 y 或 n。')


def title_prefix_from_keyword(keyword: str) -> str:
    import re
    return re.sub(r'[^0-9A-Za-z]+', '_', keyword.lower()).strip('_') or 'idp_image'


def keyword_changed(old_keyword: str, new_keyword: str) -> bool:
    return title_prefix_from_keyword(old_keyword) != title_prefix_from_keyword(new_keyword)


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


def clear_stale_run_lock(cache_dir: Path) -> bool:
    """轮次之间 supervisor 独占 cache_dir，此时残留的 run.lock 一定是上一轮（可能被硬杀）遗留的。
    若其 PID 已不存在则删除，避免 PID 复用导致 main.py 误判 cache_is_locked 而下到 ImagesCache_xx，
    与 supervisor 读取的目录产生分叉、虚报 0 进度。"""
    lock_file = cache_dir / 'run.lock'
    if not lock_file.exists():
        return False
    if cache_is_locked(cache_dir):
        return False
    try:
        lock_file.unlink()
        return True
    except OSError:
        return False


def should_resume_round(*, resume_first_round: bool, downloaded_count: int) -> bool:
    return resume_first_round or downloaded_count > 0


def archive_cache(cache_dir: Path, keyword: str) -> dict:
    if cache_is_locked(cache_dir):
        raise RuntimeError(f'ImagesCache is locked by a running process: {cache_dir}')
    if not cache_has_content(cache_dir):
        cache_dir.mkdir(parents=True, exist_ok=True)
        return {'archived': False, 'reason': 'empty_cache', 'cache_dir': str(cache_dir)}

    images_root = cache_dir.parent
    archive_name = title_prefix_from_keyword(keyword)
    target_dir = images_root / archive_name
    if target_dir.exists():
        target_dir = images_root / f'{archive_name}_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    shutil.move(str(cache_dir), str(target_dir))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return {
        'archived': True,
        'keyword': keyword,
        'archive_dir': str(target_dir),
        'cache_dir': str(cache_dir),
    }


def archive_cache_for_keyword_change(cache_dir: Path, old_keyword: str, new_keyword: str) -> dict:
    if not keyword_changed(old_keyword, new_keyword):
        cache_dir.mkdir(parents=True, exist_ok=True)
        return {'archived': False, 'reason': 'same_keyword', 'cache_dir': str(cache_dir)}
    summary = archive_cache(cache_dir, old_keyword)
    summary.update({
        'old_keyword': old_keyword,
        'new_keyword': new_keyword,
    })
    return summary


def detect_search_keyword(task_text: str, default: str = 'china buddhist') -> str:
    import re
    patterns = [
        r'搜索关键词\s*\*\*`([^`]+)`\*\*',
        r'搜索关键词固定为\s*`([^`]+)`',
        r'关键词\s*`([^`]+)`',
        r'keyword="([^"]+)"',
        r"keyword='([^']+)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, task_text, re.IGNORECASE)
        if match and match.group(1).strip():
            return re.sub(r'\s+', ' ', match.group(1)).strip()
    return default


def _replace_whole_word(text: str, old: str, new: str) -> str:
    """只替换作为完整词出现的 old，避免把 old 当子串误伤其它单词。

    例如 old='si'、new='miao' 时，绝不能把 'session' 改成 'sesmiaoon'。
    用词边界 \\b 包裹 old；若 old 含正则元字符或非词字符（如短语、含空格），
    则退回到带边界断言的转义匹配。
    """
    import re
    if not old or old == new:
        return text
    pattern = r'(?<![0-9A-Za-z])' + re.escape(old) + r'(?![0-9A-Za-z])'
    return re.sub(pattern, lambda _m: new, text)


def update_task_search_keyword(task_file: Path, new_keyword: str) -> dict:
    import re
    if not task_file.exists():
        raise FileNotFoundError(f'task.md not found: {task_file}')
    text = task_file.read_text(encoding='utf-8')
    old_keyword = detect_search_keyword(text)
    old_prefix = title_prefix_from_keyword(old_keyword)
    new_keyword = re.sub(r'\s+', ' ', new_keyword).strip()
    if not new_keyword:
        raise ValueError('搜索词不能为空')
    new_prefix = title_prefix_from_keyword(new_keyword)

    replacements = {
        old_keyword: new_keyword,
        old_prefix: new_prefix,
    }
    for old, new in replacements.items():
        text = _replace_whole_word(text, old, new)

    task_file.write_text(text, encoding='utf-8')
    return {
        'task_file': str(task_file),
        'old_keyword': old_keyword,
        'new_keyword': new_keyword,
        'old_title_prefix': old_prefix,
        'new_title_prefix': new_prefix,
    }


def update_cache_keyword(cache_dir: Path, keyword: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for filename in ('idp_progress.json', 'run_config.json'):
        path = cache_dir / filename
        try:
            data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data['keyword'] = keyword
        data['title_prefix'] = title_prefix_from_keyword(keyword)
        data['updated_at'] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize_progress_page(
    cache_dir: Path,
    *,
    max_reasonable_page: int,
    fallback_page: int,
) -> dict:
    """
    Prevent LLM recovery loops from leaving the checkpoint on extreme pages like 999/5000.
    The downloader should advance pages deterministically; deep jumps are treated as bad state.
    """
    progress_path = cache_dir / 'idp_progress.json'
    if not progress_path.exists():
        return {'changed': False, 'reason': 'missing_progress'}
    try:
        progress = json.loads(progress_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {'changed': False, 'reason': 'invalid_json'}
    if not isinstance(progress, dict):
        return {'changed': False, 'reason': 'invalid_progress'}

    try:
        next_page = int(progress.get('next_page') or progress.get('current_page') or 1)
    except (TypeError, ValueError):
        next_page = 1
    try:
        current_page = int(progress.get('current_page') or next_page)
    except (TypeError, ValueError):
        current_page = next_page

    if next_page <= max_reasonable_page and current_page <= max_reasonable_page:
        return {'changed': False, 'next_page': next_page, 'current_page': current_page}

    old = {'current_page': current_page, 'next_page': next_page, 'next_index': progress.get('next_index')}
    progress.update({
        'current_page': fallback_page,
        'next_page': fallback_page,
        'next_index': 0,
        'source': f'normalized_extreme_page_over_{max_reasonable_page}',
        'previous_bad_page': old,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'changed': True, 'old': old, 'new_page': fallback_page, 'max_reasonable_page': max_reasonable_page}


def sync_progress_from_page_queue(
    cache_dir: Path,
    *,
    target_count: int,
    max_reasonable_page: int,
    fallback_page: int,
) -> dict:
    keyword = detect_search_keyword(TASK_FILE.read_text(encoding='utf-8')) if TASK_FILE.exists() else 'china buddhist'
    active = select_next_page(
        cache_dir,
        keyword=keyword,
        target_count=target_count,
        fallback_page=fallback_page,
        max_reasonable_page=max_reasonable_page,
    )
    progress_path = cache_dir / 'idp_progress.json'
    try:
        progress = json.loads(progress_path.read_text(encoding='utf-8')) if progress_path.exists() else {}
    except json.JSONDecodeError:
        progress = {}
    if not isinstance(progress, dict):
        progress = {}
    progress.update({
        'keyword': keyword,
        'target_count': target_count,
        'current_page': active['page'],
        'next_page': active['page'],
        'next_index': active['next_index'],
        'source': 'synced_from_idp_page_progress',
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
    return active


def apply_keyword_change(cache_dir: Path, new_keyword: str) -> dict | None:
    """非交互地把搜索词切换为 new_keyword：归档旧缓存、改写 task.md、更新缓存内关键词。
    若关键词未变化则返回 None（不做任何破坏性操作）。"""
    new_keyword = (new_keyword or '').strip()
    if not new_keyword:
        raise ValueError('搜索词不能为空')
    old_keyword = detect_search_keyword(TASK_FILE.read_text(encoding='utf-8')) if TASK_FILE.exists() else 'china buddhist'
    if not keyword_changed(old_keyword, new_keyword):
        return None
    archive_summary = archive_cache_for_keyword_change(cache_dir, old_keyword, new_keyword)
    summary = update_task_search_keyword(TASK_FILE, new_keyword)
    update_cache_keyword(cache_dir, summary['new_keyword'])
    result = {'search_update': summary, 'cache_archive': archive_summary}
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def configure_before_run(
    cache_dir: Path,
    resume_choice: bool | None = None,
    *,
    keyword_override: str | None = None,
    target_override: int | None = None,
) -> bool:
    # 非交互模式：由 GUI/脚本通过参数传入关键词/目标，或在没有 TTY 时不再阻塞 input()。
    non_interactive = (
        keyword_override is not None
        or target_override is not None
        or not sys.stdin.isatty()
    )

    if keyword_override is not None:
        apply_keyword_change(cache_dir, keyword_override)
    elif not non_interactive and yes_or_no('是否要修改搜索词？[y/N]: '):
        new_keyword = input('请输入新的搜索词: ').strip()
        apply_keyword_change(cache_dir, new_keyword)

    if target_override is not None:
        summary = configure_target(cache_dir, int(target_override), update_task=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    elif not non_interactive and yes_or_no('是否要修改目标下载数量？[y/N]: '):
        target_text = input('请输入新的目标总下载数量: ').strip()
        target_count = int(target_text)
        summary = configure_target(cache_dir, target_count, update_task=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    if resume_choice is not None:
        print('♻️ 已通过参数选择断点续跑' if resume_choice else '🆕 已通过参数选择从头开始')
        return resume_choice
    if non_interactive:
        # 没有显式 --resume/--new-run 时，非交互默认从头开始，避免阻塞在 input()。
        return False
    return yes_or_no('是否从上次断点续跑？[y/N]: ')


def start_quit_listener(stop_event: threading.Event) -> threading.Thread:
    """
    Listen for "quit" in the supervisor process. The child main.py input monitor is disabled
    while supervised so this is the single owner of terminal input.
    """
    def listen() -> None:
        print("\n💡 自动运行中输入 'quit' 并回车可停止当前轮并退出自动重启。\n", flush=True)
        while not stop_event.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            if line.strip().lower() == 'quit':
                print("\n⚠️ 收到 quit，正在停止当前 main.py 子进程并退出自动运行...\n", flush=True)
                stop_event.set()
                return

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    return thread


def read_target_from_task(task_file: Path = TASK_FILE) -> int:
    if not task_file.exists():
        raise FileNotFoundError(f'task.md not found: {task_file}')
    target = detect_task_target(task_file.read_text(encoding='utf-8'))
    if target is None:
        raise ValueError(f'Cannot find target count in {task_file}')
    return target


def read_downloaded_count(cache_dir: Path = DEFAULT_CACHE_DIR) -> int:
    record_file = cache_dir / 'image_record.jsonl'
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
        if isinstance(record, dict) and record.get('status') == 'downloaded':
            count += 1
    return count


def import_sqlite(cache_dir: Path) -> None:
    script = BASE_DIR / 'import_records_to_sqlite.py'
    if not script.exists():
        return
    subprocess.run(
        [
            sys.executable,
            str(script),
            '--record-file',
            str(cache_dir / 'image_record.jsonl'),
            '--image-dir',
            str(cache_dir),
            '--db-file',
            str(cache_dir / 'image_catalog.sqlite3'),
            '--json',
        ],
        cwd=str(BASE_DIR),
        env={**os.environ.copy(), 'PYTHONIOENCODING': 'utf-8'},
        check=False,
    )


def run_main_subprocess(
    cache_dir: Path,
    round_number: int,
    stop_event: threading.Event,
    *,
    resume_run: bool,
    round_timeout: int = 0,
) -> int:
    log_file = cache_dir / 'auto_runner.log' if resume_run else BASE_DIR / f'.auto_runner_round_{os.getpid()}_{round_number}.log'
    env = {
        **os.environ.copy(),
        'BROWSER_USE_RESUME_RUN': '1' if resume_run else '0',
        'BROWSER_USE_RUN_DIR': str(cache_dir.resolve()),
        'BROWSER_USE_IMAGE_DIR': str(cache_dir.resolve()),
        'BROWSER_USE_AGENT_DATA_DIR': str(cache_dir.resolve()),
        'BROWSER_USE_LLM_TIMEOUT': os.environ.get('BROWSER_USE_LLM_TIMEOUT', '180'),
        'BROWSER_USE_STEP_TIMEOUT': os.environ.get('BROWSER_USE_STEP_TIMEOUT', '240'),
        'BROWSER_USE_DISABLE_INPUT_MONITOR': '1',
        'PYTHONIOENCODING': 'utf-8',
    }
    # POSIX 下新建会话，使整个子进程树共享进程组，便于 os.killpg 兜底清理。
    popen_kwargs: dict = {}
    if os.name != 'nt':
        popen_kwargs['start_new_session'] = True

    with log_file.open('a', encoding='utf-8') as log:
        start_line = f'\n=== auto round {round_number} started {datetime.now(timezone.utc).isoformat()} ==='
        print(start_line, flush=True)
        log.write(start_line + '\n')
        log.flush()
        process = subprocess.Popen(
            [sys.executable, str(BASE_DIR / 'main.py')],
            cwd=str(BASE_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            **popen_kwargs,
        )

        # 进程退出后用于停掉两个看门狗线程，避免它们空转。
        finished_event = threading.Event()
        timed_out = threading.Event()

        def stop_child_on_quit() -> None:
            # 等待用户 quit 或进程自然结束，二者先到先停。
            while not finished_event.is_set():
                if stop_event.wait(timeout=0.5):
                    if process.poll() is None:
                        terminate_process_tree(process)
                    return

        def kill_on_timeout() -> None:
            if round_timeout <= 0:
                return
            if finished_event.wait(timeout=round_timeout):
                return  # 进程已正常结束
            if process.poll() is None:
                timed_out.set()
                msg = f'\n⏱️ 第 {round_number} 轮超过 {round_timeout}s 仍未结束，判定卡死，清理进程树...'
                print(msg, flush=True)
                log.write(msg + '\n')
                log.flush()
                terminate_process_tree(process)

        threading.Thread(target=stop_child_on_quit, daemon=True).start()
        threading.Thread(target=kill_on_timeout, daemon=True).start()

        assert process.stdout is not None
        for line in process.stdout:
            print(line, end='', flush=True)
            log.write(line)
            log.flush()
            if stop_event.is_set() and process.poll() is None:
                terminate_process_tree(process)
        returncode = process.wait()
        finished_event.set()  # 通知看门狗线程退出
        # 进程退出后，确保没有残留的孤儿子进程（Chromium）继续占用 profile 锁。
        terminate_process_tree(process)
        status = 'timeout' if timed_out.is_set() else str(returncode)
        end_line = f'\n=== auto round {round_number} exited {status} {datetime.now(timezone.utc).isoformat()} ==='
        print(end_line, flush=True)
        log.write(end_line + '\n')
    if not resume_run and log_file.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        with (cache_dir / 'auto_runner.log').open('a', encoding='utf-8') as cache_log:
            cache_log.write(log_file.read_text(encoding='utf-8'))
        log_file.unlink()
    return int(returncode)


def auto_run_until_target(
    *,
    cache_dir: Path,
    resume_first_round: bool,
    max_rounds: int,
    max_no_progress_rounds: int,
    sleep_seconds: int,
    max_reasonable_page: int,
    fallback_page: int,
    round_timeout: int = 0,
) -> dict:
    target = read_target_from_task()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if resume_first_round:
        configure_target(cache_dir, target, update_task=False)
    else:
        keyword = detect_search_keyword(TASK_FILE.read_text(encoding='utf-8')) if TASK_FILE.exists() else 'china buddhist'
        archive_summary = archive_cache(cache_dir, keyword)
        if archive_summary.get('archived'):
            print(f"🆕 已归档旧缓存并从头开始: {json.dumps(archive_summary, ensure_ascii=False)}", flush=True)

    stop_event = threading.Event()
    start_quit_listener(stop_event)
    no_progress_rounds = 0
    history: list[dict] = []

    for round_number in range(1, max_rounds + 1):
        if stop_event.is_set():
            break
        if clear_stale_run_lock(cache_dir):
            print("🧹 已清理上一轮遗留的 run.lock（PID 已退出）", flush=True)
        target = read_target_from_task()
        before = read_downloaded_count(cache_dir)
        if before >= target:
            break

        resume_this_round = should_resume_round(resume_first_round=resume_first_round, downloaded_count=before)
        if resume_this_round:
            configure_target(cache_dir, target, update_task=False)
            normalize_result = normalize_progress_page(
                cache_dir,
                max_reasonable_page=max_reasonable_page,
                fallback_page=fallback_page,
            )
            if normalize_result.get('changed'):
                print(f"⚠️ 已修正异常续跑页: {json.dumps(normalize_result, ensure_ascii=False)}", flush=True)
            active_page = sync_progress_from_page_queue(
                cache_dir,
                target_count=target,
                max_reasonable_page=max_reasonable_page,
                fallback_page=fallback_page,
            )
            print(f"📌 页面队列选择续跑点: page={active_page['page']}, index={active_page['next_index']}", flush=True)
        else:
            print("🆕 本轮选择从头开始，不读取或同步续跑点", flush=True)

        returncode = run_main_subprocess(
            cache_dir, round_number, stop_event, resume_run=resume_this_round, round_timeout=round_timeout
        )
        if returncode == LLM_BLOCKED_EXIT_CODE:
            print(
                '🛑 子进程报告 LLM 端点被拦截（退出码 3），停止自动重试。'
                '请连接校园网/VPN 或检查 OPENAI_API_KEY / OPENAI_BASE_URL 后重新运行。',
                flush=True,
            )
            history.append({
                'round': round_number,
                'target': target,
                'before': before,
                'after': read_downloaded_count(cache_dir),
                'delta': 0,
                'returncode': returncode,
                'resume_run': resume_this_round,
                'fatal': 'llm_endpoint_blocked',
                'finished_at': datetime.now(timezone.utc).isoformat(),
            })
            break
        import_sqlite(cache_dir)
        configure_target(cache_dir, target, update_task=False)
        after = read_downloaded_count(cache_dir)
        delta = after - before
        history.append({
            'round': round_number,
            'target': target,
            'before': before,
            'after': after,
            'delta': delta,
            'returncode': returncode,
            'resume_run': resume_this_round,
            'finished_at': datetime.now(timezone.utc).isoformat(),
        })

        if after >= target:
            break
        if stop_event.is_set():
            break
        if delta <= 0:
            no_progress_rounds += 1
        else:
            no_progress_rounds = 0
        if no_progress_rounds >= max_no_progress_rounds:
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    final_count = read_downloaded_count(cache_dir)
    summary = {
        'target': read_target_from_task(),
        'downloaded_records': final_count,
        'remaining_records': max(0, read_target_from_task() - final_count),
        'max_rounds': max_rounds,
        'max_no_progress_rounds': max_no_progress_rounds,
        'resume_first_round': resume_first_round,
        'max_reasonable_page': max_reasonable_page,
        'fallback_page': fallback_page,
        'round_timeout': round_timeout,
        'history': history,
        'completed': final_count >= read_target_from_task(),
    }
    (cache_dir / 'auto_runner_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Auto-run main.py until task.md target is reached.')
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument('--keyword', type=str, default=None, help='非交互地设置搜索词；与当前不同则归档旧缓存并改写 task.md')
    parser.add_argument('--target', type=int, default=None, help='非交互地设置目标下载总数并写入 task.md')
    parser.add_argument('--max-rounds', type=int, default=100)
    parser.add_argument('--max-no-progress-rounds', type=int, default=3)
    parser.add_argument('--sleep-seconds', type=int, default=5)
    parser.add_argument('--max-reasonable-page', type=int, default=200, help='超过该页码的断点会被视为异常跳页')
    parser.add_argument('--fallback-page', type=int, default=1, help='发现异常跳页且没有可靠续跑点时从该页重新开始')
    parser.add_argument(
        '--round-timeout',
        type=int,
        default=int(os.environ.get('BROWSER_USE_ROUND_TIMEOUT', '0')),
        help='单轮 main.py 的最长运行秒数，超时判定卡死并清理进程树；<=0 表示不限制',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--resume', action='store_true', help='第一轮从上次断点续跑')
    mode.add_argument('--new-run', action='store_true', help='第一轮归档旧 ImagesCache 并从头开始')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    resume_choice = True if args.resume else False if args.new_run else None
    resume_first_round = configure_before_run(
        args.cache_dir,
        resume_choice=resume_choice,
        keyword_override=args.keyword,
        target_override=args.target,
    )
    summary = auto_run_until_target(
        cache_dir=args.cache_dir,
        resume_first_round=resume_first_round,
        max_rounds=args.max_rounds,
        max_no_progress_rounds=args.max_no_progress_rounds,
        sleep_seconds=args.sleep_seconds,
        max_reasonable_page=args.max_reasonable_page,
        fallback_page=args.fallback_page,
        round_timeout=args.round_timeout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['completed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
