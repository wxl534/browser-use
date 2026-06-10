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
        if old and old != new:
            text = text.replace(old, new)

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


def configure_before_run(cache_dir: Path, resume_choice: bool | None = None) -> bool:
    if yes_or_no('是否要修改搜索词？[y/N]: '):
        new_keyword = input('请输入新的搜索词: ').strip()
        if not new_keyword:
            raise ValueError('搜索词不能为空')
        old_keyword = detect_search_keyword(TASK_FILE.read_text(encoding='utf-8')) if TASK_FILE.exists() else 'china buddhist'
        archive_summary = archive_cache_for_keyword_change(cache_dir, old_keyword, new_keyword)
        summary = update_task_search_keyword(TASK_FILE, new_keyword)
        update_cache_keyword(cache_dir, summary['new_keyword'])
        print(json.dumps({'search_update': summary, 'cache_archive': archive_summary}, ensure_ascii=False, indent=2))

    if yes_or_no('是否要修改目标下载数量？[y/N]: '):
        target_text = input('请输入新的目标总下载数量: ').strip()
        target_count = int(target_text)
        summary = configure_target(cache_dir, target_count, update_task=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    if resume_choice is not None:
        print('♻️ 已通过参数选择断点续跑' if resume_choice else '🆕 已通过参数选择从头开始')
        return resume_choice
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


def run_main_subprocess(cache_dir: Path, round_number: int, stop_event: threading.Event, *, resume_run: bool) -> int:
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
        )
        def stop_child_on_quit() -> None:
            stop_event.wait()
            if process.poll() is None:
                process.terminate()

        threading.Thread(target=stop_child_on_quit, daemon=True).start()
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end='', flush=True)
            log.write(line)
            log.flush()
            if stop_event.is_set() and process.poll() is None:
                process.terminate()
        returncode = process.wait()
        end_line = f'\n=== auto round {round_number} exited {returncode} {datetime.now(timezone.utc).isoformat()} ==='
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
    previous_count = read_downloaded_count(cache_dir)

    for round_number in range(1, max_rounds + 1):
        if stop_event.is_set():
            break
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

        returncode = run_main_subprocess(cache_dir, round_number, stop_event, resume_run=resume_this_round)
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
        previous_count = after
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
        'history': history,
        'completed': final_count >= read_target_from_task(),
    }
    (cache_dir / 'auto_runner_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Auto-run main.py until task.md target is reached.')
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument('--max-rounds', type=int, default=100)
    parser.add_argument('--max-no-progress-rounds', type=int, default=3)
    parser.add_argument('--sleep-seconds', type=int, default=5)
    parser.add_argument('--max-reasonable-page', type=int, default=200, help='超过该页码的断点会被视为异常跳页')
    parser.add_argument('--fallback-page', type=int, default=1, help='发现异常跳页且没有可靠续跑点时从该页重新开始')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--resume', action='store_true', help='第一轮从上次断点续跑')
    mode.add_argument('--new-run', action='store_true', help='第一轮归档旧 ImagesCache 并从头开始')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    resume_choice = True if args.resume else False if args.new_run else None
    resume_first_round = configure_before_run(args.cache_dir, resume_choice=resume_choice)
    summary = auto_run_until_target(
        cache_dir=args.cache_dir,
        resume_first_round=resume_first_round,
        max_rounds=args.max_rounds,
        max_no_progress_rounds=args.max_no_progress_rounds,
        sleep_seconds=args.sleep_seconds,
        max_reasonable_page=args.max_reasonable_page,
        fallback_page=args.fallback_page,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['completed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
