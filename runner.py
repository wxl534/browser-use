"""
Run browser-use repeatedly until ImagesCache reaches the target in task.md.

This supervisor intentionally starts worker.py as a fresh subprocess each round.
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

sys.path.insert(0, str(Path(__file__).resolve().parent / 'core'))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))

from configure_resume_target import DEFAULT_CACHE_DIR, TASK_FILE, configure_target, detect_task_target
from idp_page_progress import select_next_page
from cache_layout import cache_has_content, cache_is_locked, process_is_running
from task_parse import detect_search_keyword, keyword_changed, title_prefix_from_keyword


BASE_DIR = Path(__file__).resolve().parent

# worker.py 用退出码 3 表示 LLM 端点被网关/门户拦截(致命,不可重试).
LLM_BLOCKED_EXIT_CODE = 3

# 单次运行开关:改为 1 则只跑一轮 worker.py 后立即停止(无论是否达标);
# 默认 0 表示持续重跑直到 task.md 目标达成.仅供运行前手动改源码使用.
RUN_ONCE = 0


def terminate_process_tree(process: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    """终止子进程及其全部后代(如 main.py 启动的 Chromium).

    仅 process.terminate() 在 Windows 上只杀 main.py,会留下孤儿 Chrome 占住
    browser_profile 锁,导致下一轮起不来.这里优先用 psutil 递归清理,
    失败时回退到 Windows 的 `taskkill /T /F /PID` 或 POSIX 的进程组信号.
    """
    if process.poll() is not None:
        return
    pid = process.pid

    # 首选:psutil 递归终止(browser-use 运行时本就依赖 psutil,跨平台最稳).
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

    # 回退:按平台杀进程树.
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
        # 最后兜底:至少杀掉直接子进程.
        try:
            process.kill()
        except Exception:
            pass


def _interruptible_sleep(seconds: float, stop_event: threading.Event) -> None:
    """可被退出事件打断的睡眠,避免长冷却期间无法响应 q 退出."""
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        if stop_event.is_set():
            return
        time.sleep(min(1.0, end - time.monotonic()))


def yes_or_no(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in {'y', 'yes', '是'}:
            return True
        if answer in {'n', 'no', '否', ''}:
            return False
        print('请输入 y 或 n.')


def clear_stale_run_lock(cache_dir: Path) -> bool:
    """轮次之间 supervisor 独占 cache_dir,此时残留的 run.lock 一定是上一轮(可能被硬杀)遗留的.
    若其 PID 已不存在则删除,避免 PID 复用导致 main.py 误判 cache_is_locked 而下到 ImagesCache_xx,
    与 supervisor 读取的目录产生分叉,虚报 0 进度."""
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


def _replace_whole_word(text: str, old: str, new: str) -> str:
    """只替换作为完整词出现的 old,避免把 old 当子串误伤其它单词.

    例如 old='si',new='miao' 时,绝不能把 'session' 改成 'sesmiaoon'.
    用词边界 \\b 包裹 old;若 old 含正则元字符或非词字符(如短语,含空格),
    则退回到带边界断言的转义匹配.
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
    """非交互地把搜索词切换为 new_keyword:归档旧缓存,改写 task.md,更新缓存内关键词.
    若关键词未变化则返回 None(不做任何破坏性操作)."""
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
    site_override: str | None = None,
    allowed_hosts_override: str | None = None,
    mode_override: str | None = None,
    item_selector_override: str | None = None,
    force_generic_override: bool | None = None,
) -> bool:
    import re

    import render_task

    # 非交互模式:由 GUI/脚本通过任一任务参数传入,或在没有 TTY 时不再阻塞 input().
    non_interactive = (
        any(value is not None for value in (
            keyword_override, target_override, site_override, allowed_hosts_override,
            mode_override, item_selector_override, force_generic_override,
        ))
        or not sys.stdin.isatty()
    )

    cfg = render_task.load_config()
    # 与现有 task.md 保持连续:未显式覆盖时,沿用 task.md 当前的关键词/目标,
    # 避免渲染把运行中途改过的值重置回 task_config.json 旧值.
    if TASK_FILE.exists():
        task_text = TASK_FILE.read_text(encoding='utf-8')
        current_keyword = detect_search_keyword(task_text)
        current_target = detect_task_target(task_text)
        if keyword_override is None and current_keyword:
            cfg['keyword'] = current_keyword
        if target_override is None and current_target:
            cfg['target_count'] = current_target

    # 搜索词:变更则归档旧缓存 + 更新缓存内关键词(task.md 最终由渲染器统一写).
    new_keyword = keyword_override
    if new_keyword is None and not non_interactive and yes_or_no('是否要修改搜索词?[y/N]: '):
        new_keyword = input('请输入新的搜索词: ').strip()
    if new_keyword:
        new_keyword = re.sub(r'\s+', ' ', new_keyword).strip()
        old_keyword = cfg['keyword']
        if keyword_changed(old_keyword, new_keyword):
            archive_cache_for_keyword_change(cache_dir, old_keyword, new_keyword)
            update_cache_keyword(cache_dir, new_keyword)
        cfg['keyword'] = new_keyword

    # 目标值:更新缓存断点状态(idp_progress / run_config);task.md 由渲染器统一写.
    new_target = target_override
    if new_target is None and not non_interactive and yes_or_no('是否要修改目标下载数量?[y/N]: '):
        new_target = int(input('请输入新的目标总下载数量: ').strip())
    if new_target is not None:
        summary = configure_target(cache_dir, int(new_target), update_task=False)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        cfg['target_count'] = int(new_target)

    # 其它任务参数(站点 / 白名单 / 模式 / item 选择器 / force_generic).
    if site_override is not None:
        cfg['site_url'] = site_override
    if allowed_hosts_override is not None:
        cfg['allowed_hosts'] = [h.strip() for h in allowed_hosts_override.split(',') if h.strip()]
    if mode_override is not None:
        cfg['mode'] = mode_override
    if item_selector_override is not None:
        cfg['item_selector'] = item_selector_override
    if force_generic_override is not None:
        cfg['force_generic'] = bool(force_generic_override)

    # 单一真相源写回 + 权威渲染 task.md(task.md 不再手改).
    cfg = render_task.normalize_config(cfg)
    render_task.save_config(cfg)
    render_task.render_to_task(cfg)
    print(f'📝 已从 task_template.md 渲染 task.md（keyword={cfg["keyword"]!r}, '
          f'target={cfg["target_count"]}, mode={cfg["mode"]}, force_generic={cfg["force_generic"]}）', flush=True)

    if resume_choice is not None:
        print('♻️ 已通过参数选择断点续跑' if resume_choice else '🆕 已通过参数选择从头开始')
        return resume_choice
    if non_interactive:
        # 没有显式 --resume/--new-run 时,非交互默认从头开始,避免阻塞在 input().
        return False
    return yes_or_no('是否从上次断点续跑?[y/N]: ')


def start_quit_listener(stop_event: threading.Event) -> threading.Thread:
    """
    Listen for "quit" in the supervisor process. The child main.py input monitor is disabled
    while supervised so this is the single owner of terminal input.
    """
    def listen() -> None:
        print("\n💡 自动运行中输入 'quit' 并回车可停止当前轮并退出自动重启.\n", flush=True)
        while not stop_event.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            if line.strip().lower() == 'quit':
                print("\n⚠️ 收到 quit,正在停止当前 main.py 子进程并退出自动运行...\n", flush=True)
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
    # POSIX 下新建会话,使整个子进程树共享进程组,便于 os.killpg 兜底清理.
    popen_kwargs: dict = {}
    if os.name != 'nt':
        popen_kwargs['start_new_session'] = True

    with log_file.open('a', encoding='utf-8') as log:
        start_line = f'\n=== auto round {round_number} started {datetime.now(timezone.utc).isoformat()} ==='
        print(start_line, flush=True)
        log.write(start_line + '\n')
        log.flush()
        process = subprocess.Popen(
            [sys.executable, str(BASE_DIR / 'worker.py')],
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

        # 进程退出后用于停掉两个看门狗线程,避免它们空转.
        finished_event = threading.Event()
        timed_out = threading.Event()

        def stop_child_on_quit() -> None:
            # 等待用户 quit 或进程自然结束,二者先到先停.
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
        # 进程退出后,确保没有残留的孤儿子进程(Chromium)继续占用 profile 锁.
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


# === 方案C:自动刷新 Cloudflare 通行证(cf_clearance)===
# 由 fetch_cf_cookie.py(在隔离的 Scrapling 解释器里)取证,产出 storage_state JSON,
# 通过 IDP_STORAGE_STATE 透传给 main.py 子进程注入.整套功能完全可选:未配置
# IDP_SCRAPLING_PYTHON 时静默关闭,不影响原有行为.
CF_REFRESH_MARGIN_SECONDS = 180  # cf_clearance 距过期不足该秒数即视为需要刷新


def _storage_state_path() -> Path:
    """storage_state 输出/读取路径(默认脚本目录下 cf_storage.json,相对脚本位置)."""
    configured = os.environ.get('IDP_STORAGE_STATE', '').strip()
    return Path(configured) if configured else (BASE_DIR / 'cf_storage.json')


def _scrapling_python() -> str | None:
    """返回可用于运行 fetch_cf_cookie.py 的 Scrapling 解释器路径;未配置/不存在则 None."""
    configured = os.environ.get('IDP_SCRAPLING_PYTHON', '').strip()
    if not configured:
        return None
    if not Path(configured).exists():
        print(f"⚠️ IDP_SCRAPLING_PYTHON 指向的解释器不存在：{configured}，自动刷新 cookie 已关闭", flush=True)
        return None
    return configured


def _cf_cookie_fresh(path: Path, margin: int = CF_REFRESH_MARGIN_SECONDS) -> bool:
    """判断现有 storage_state 是否仍持有未临近过期的 cf_clearance."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return False
    meta = data.get('_meta', {}) if isinstance(data, dict) else {}
    if not meta.get('cf_clearance_present'):
        return False
    expires = meta.get('cf_clearance_expires')
    if not isinstance(expires, (int, float)) or expires <= 0:
        # 无过期信息时保守地认为新鲜(避免每轮都刷),靠 force 刷新兜底.
        return True
    return time.time() < (expires - margin)


def ensure_cf_cookie(stop_event: threading.Event, *, force: bool = False, reason: str = '') -> None:
    """确保 storage_state 持有有效 cf_clearance;过期/缺失或 force 时调用取证脚本刷新.

    取证脚本继承当前进程的环境变量(含 IDP_PROXY_* / IDP_CF_URL / IDP_STORAGE_STATE),
    因此取证与 browser-use 共用同一代理 / 出口 IP -- 这是 cf_clearance 生效的前提.
    """
    python = _scrapling_python()
    if python is None:
        return  # 功能未启用,静默跳过
    if stop_event.is_set():
        return
    path = _storage_state_path()
    if not force and _cf_cookie_fresh(path):
        return

    label = reason or ('强制刷新' if force else '刷新')
    print(f"🍪 [{label}] 调用 Scrapling 取证刷新 cf_clearance …", flush=True)
    env = {**os.environ.copy(), 'PYTHONIOENCODING': 'utf-8', 'IDP_STORAGE_STATE': str(path)}
    try:
        proc = subprocess.run(
            [python, str(BASE_DIR / 'fetch_cf_cookie.py')],
            cwd=str(BASE_DIR),
            env=env,
            timeout=int(os.environ.get('IDP_CF_FETCH_TIMEOUT', '300')),
        )
    except subprocess.TimeoutExpired:
        print("⚠️ cf_clearance 取证超时,本轮沿用现有 cookie(若有).", flush=True)
        return
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ cf_clearance 取证调用失败：{type(exc).__name__}: {exc}", flush=True)
        return
    if proc.returncode == 0:
        print(f"✅ cf_clearance 已刷新 → {path}", flush=True)
    elif proc.returncode == 2:
        print("⚠️ 取证完成但未获 cf_clearance(可能本就放行,或 IP 信誉被挡需换住宅代理).", flush=True)
    else:
        print(f"⚠️ 取证脚本返回码 {proc.returncode}，本轮沿用现有 cookie（若有）。", flush=True)


def auto_run_until_target(
    *,
    cache_dir: Path,
    resume_first_round: bool,
    max_rounds: int,
    max_no_progress_rounds: int,
    sleep_seconds: int,
    cooldown_seconds: int,
    cooldown_max_seconds: int,
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

    # 方案C:若启用了 Scrapling 取证(设了 IDP_SCRAPLING_PYTHON),让 main.py 子进程
    # 读到 storage_state 路径,并在开跑前预取一次 cf_clearance 通行证.
    if _scrapling_python() is not None:
        os.environ['IDP_STORAGE_STATE'] = str(_storage_state_path())
        ensure_cf_cookie(stop_event, reason='启动前预取')

    for round_number in range(1, max_rounds + 1):
        if stop_event.is_set():
            break
        if clear_stale_run_lock(cache_dir):
            print("🧹 已清理上一轮遗留的 run.lock(PID 已退出)", flush=True)
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
            print("🆕 本轮选择从头开始,不读取或同步续跑点", flush=True)

        # 方案C:跑前确保通行证新鲜(过期/缺失才会真正去刷,否则秒返回).
        ensure_cf_cookie(stop_event, reason=f'第{round_number}轮跑前检查')

        returncode = run_main_subprocess(
            cache_dir, round_number, stop_event, resume_run=resume_this_round, round_timeout=round_timeout
        )
        if returncode == LLM_BLOCKED_EXIT_CODE:
            print(
                '🛑 子进程报告 LLM 端点被拦截(退出码 3),停止自动重试.'
                '请连接校园网/VPN 或检查 OPENAI_API_KEY / OPENAI_BASE_URL 后重新运行.',
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
        if RUN_ONCE:
            print("🔂 RUN_ONCE=1:已完成单次运行,按设置停止(不再继续重跑)", flush=True)
            break
        if stop_event.is_set():
            break
        if delta <= 0:
            no_progress_rounds += 1
        else:
            no_progress_rounds = 0
        if no_progress_rounds >= max_no_progress_rounds:
            break
        # 自适应冷却:本轮无新增通常意味着被 Cloudflare 限流,递增退避等待限流窗口恢复;
        # 有新增时只做正常的轻量间隔.
        if no_progress_rounds > 0 and cooldown_seconds > 0:
            cooldown = min(cooldown_seconds * (2 ** (no_progress_rounds - 1)), cooldown_max_seconds)
            print(
                f"🧊 第 {round_number} 轮无新增（可能被反爬限流），冷却 {cooldown}s "
                f"让限流窗口恢复后再续跑…（连续无进展 {no_progress_rounds}/{max_no_progress_rounds}）",
                flush=True,
            )
            # 方案C:无新增多半是 cf_clearance 失效或本会话被挑战,强制重新取证一张通行证.
            ensure_cf_cookie(stop_event, force=True, reason='疑似被限流,强制刷新通行证')
            _interruptible_sleep(cooldown, stop_event)
        elif sleep_seconds > 0:
            _interruptible_sleep(sleep_seconds, stop_event)

    final_count = read_downloaded_count(cache_dir)
    summary = {
        'target': read_target_from_task(),
        'downloaded_records': final_count,
        'remaining_records': max(0, read_target_from_task() - final_count),
        'max_rounds': max_rounds,
        'max_no_progress_rounds': max_no_progress_rounds,
        'cooldown_seconds': cooldown_seconds,
        'cooldown_max_seconds': cooldown_max_seconds,
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
    parser.add_argument('--keyword', type=str, default=None, help='非交互地设置搜索词;与当前不同则归档旧缓存并改写 task.md')
    parser.add_argument('--target', type=int, default=None, help='非交互地设置目标下载总数并写入 task.md')
    parser.add_argument('--site', type=str, default=None, help='目标站点首页 URL(写入 task_config.json 并渲染 task.md)')
    parser.add_argument('--allowed-hosts', type=str, default=None, help='允许下载的域名后缀白名单,逗号分隔')
    parser.add_argument('--mode', choices=('idp_batch', 'generic_per_item'), default=None,
                        help='任务模式:idp_batch(站点专属批量)| generic_per_item(任意站点逐 item 稳定路径)')
    parser.add_argument('--item-selector', type=str, default=None, help='非注册站点的 item 详情链接 CSS 选择器(generic 模式)')
    fg = parser.add_mutually_exclusive_group()
    fg.add_argument('--force-generic', dest='force_generic', action='store_true', default=None,
                    help='通用下载跳过站点专属 manifest 加速(稳定优先)')
    fg.add_argument('--no-force-generic', dest='force_generic', action='store_false', default=None,
                    help='关闭 force_generic')
    parser.add_argument('--max-rounds', type=int, default=100)
    parser.add_argument('--max-no-progress-rounds', type=int, default=3)
    parser.add_argument('--sleep-seconds', type=int, default=5)
    parser.add_argument(
        '--cooldown-seconds',
        type=int,
        default=int(os.environ.get('BROWSER_USE_COOLDOWN_SECONDS', '60')),
        help='某一轮无新增(疑似被反爬限流)时的基础冷却秒数,按连续无进展轮次指数递增;<=0 表示沿用 --sleep-seconds',
    )
    parser.add_argument(
        '--cooldown-max-seconds',
        type=int,
        default=int(os.environ.get('BROWSER_USE_COOLDOWN_MAX_SECONDS', '600')),
        help='自适应冷却的上限秒数',
    )
    parser.add_argument(
        '--page-delay-seconds',
        type=float,
        default=float(os.environ.get('BROWSER_USE_PAGE_DELAY_SECONDS', '12')),
        help='每页批量下载前的节流延时秒数,降低触发 Cloudflare 限流概率;传给子进程的 BROWSER_USE_PAGE_DELAY_SECONDS(默认 12)',
    )
    parser.add_argument('--max-reasonable-page', type=int, default=200, help='超过该页码的断点会被视为异常跳页')
    parser.add_argument('--fallback-page', type=int, default=1, help='发现异常跳页且没有可靠续跑点时从该页重新开始')
    parser.add_argument(
        '--round-timeout',
        type=int,
        default=int(os.environ.get('BROWSER_USE_ROUND_TIMEOUT', '0')),
        help='单轮 main.py 的最长运行秒数,超时判定卡死并清理进程树;<=0 表示不限制',
    )
    # === 反爬:真实 Chrome profile + 可选代理(绕过 Cloudflare 人机验证)===
    # Cloudflare Turnstile 主要按后台指纹 + IP 信誉判定,而非"点击复选框"本身.
    # 用带真实 cookie/cf_clearance 的 Chrome profile 出场可让目标站静默放行.
    parser.add_argument(
        '--chrome-executable',
        type=str,
        default=os.environ.get('IDP_CHROME_EXECUTABLE', ''),
        help='真实 Chrome 可执行文件路径(与 --chrome-user-data-dir 同时设置才启用真实 Chrome)',
    )
    parser.add_argument(
        '--chrome-user-data-dir',
        type=str,
        default=os.environ.get('IDP_CHROME_USER_DATA_DIR', ''),
        help='真实 Chrome 用户数据目录(须先完全关闭该 Chrome,避免 profile 被占用)',
    )
    parser.add_argument(
        '--chrome-profile-directory',
        type=str,
        default=os.environ.get('IDP_CHROME_PROFILE_DIRECTORY', 'Default'),
        help="Chrome profile 子目录名(默认 'Default')",
    )
    parser.add_argument(
        '--proxy-server',
        type=str,
        default=os.environ.get('IDP_PROXY_SERVER', ''),
        help='可选代理服务器,如 http://user:pass@host:port(攻击 Cloudflare 的 IP 信誉层)',
    )
    # === 方案C:用 Scrapling 自动取 cf_clearance 通行证并注入 ===
    parser.add_argument(
        '--scrapling-python',
        type=str,
        default=os.environ.get('IDP_SCRAPLING_PYTHON', ''),
        help='安装了 scrapling[fetchers] 的解释器路径;设置后启用自动取证/刷新 cf_clearance(不设则关闭)',
    )
    parser.add_argument(
        '--cf-url',
        type=str,
        default=os.environ.get('IDP_CF_URL', ''),
        help='取 cf_clearance 的目标 URL(默认目标站首页,由 fetch_cf_cookie.py 兜底)',
    )
    parser.add_argument(
        '--storage-state',
        type=str,
        default=os.environ.get('IDP_STORAGE_STATE', ''),
        help='storage_state JSON 路径(取证输出 + main.py 注入;默认脚本目录下 cf_storage.json)',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--resume', action='store_true', help='第一轮从上次断点续跑')
    mode.add_argument('--new-run', action='store_true', help='第一轮归档旧 ImagesCache 并从头开始')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    resume_choice = True if args.resume else False if args.new_run else None
    # 让每页节流延时对子进程 main.py 生效(批量下载工具读取该环境变量).
    if args.page_delay_seconds and args.page_delay_seconds > 0:
        os.environ['BROWSER_USE_PAGE_DELAY_SECONDS'] = str(args.page_delay_seconds)
    # 透传真实 Chrome / 代理配置给子进程 main.py(build_browser 读取这些环境变量).
    if args.chrome_executable:
        os.environ['IDP_CHROME_EXECUTABLE'] = args.chrome_executable
    if args.chrome_user_data_dir:
        os.environ['IDP_CHROME_USER_DATA_DIR'] = args.chrome_user_data_dir
    if args.chrome_profile_directory:
        os.environ['IDP_CHROME_PROFILE_DIRECTORY'] = args.chrome_profile_directory
    if args.proxy_server:
        os.environ['IDP_PROXY_SERVER'] = args.proxy_server
    # 方案C:透传取证配置(auto_run 内部及 fetch_cf_cookie.py 都从这些环境变量读取).
    if args.scrapling_python:
        os.environ['IDP_SCRAPLING_PYTHON'] = args.scrapling_python
    if args.cf_url:
        os.environ['IDP_CF_URL'] = args.cf_url
    if args.storage_state:
        os.environ['IDP_STORAGE_STATE'] = args.storage_state
    resume_first_round = configure_before_run(
        args.cache_dir,
        resume_choice=resume_choice,
        keyword_override=args.keyword,
        target_override=args.target,
        site_override=args.site,
        allowed_hosts_override=args.allowed_hosts,
        mode_override=args.mode,
        item_selector_override=args.item_selector,
        force_generic_override=args.force_generic,
    )
    summary = auto_run_until_target(
        cache_dir=args.cache_dir,
        resume_first_round=resume_first_round,
        max_rounds=args.max_rounds,
        max_no_progress_rounds=args.max_no_progress_rounds,
        sleep_seconds=args.sleep_seconds,
        cooldown_seconds=args.cooldown_seconds,
        cooldown_max_seconds=args.cooldown_max_seconds,
        max_reasonable_page=args.max_reasonable_page,
        fallback_page=args.fallback_page,
        round_timeout=args.round_timeout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['completed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

