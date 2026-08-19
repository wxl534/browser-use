"""爬虫 supervisor 的进程托管 + 实时日志广播(供 Web 控制台用).

职责边界与 run_gui.py 一致:本模块只负责"用参数启动 runner.py
子进程,转发它的 stdout,按需停止",所有业务逻辑(归档,改写 task.md,断点续跑,
方案C 取证)仍由 supervisor 自己用命令行参数完成,前端不重复实现.

日志通过 asyncio 队列广播给所有 SSE 订阅者;后台读取线程用 call_soon_threadsafe
把行投递到主事件循环,避免线程/协程竞争.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from collections import deque
from typing import Any

from . import paths

try:
    from runner import LLM_BLOCKED_EXIT_CODE, terminate_process_tree
except Exception:  # pragma: no cover - 仅在 import 异常环境下退化
    LLM_BLOCKED_EXIT_CODE = 3

    def terminate_process_tree(process, *, grace_seconds: float = 5.0):  # type: ignore
        process.terminate()


class RunManager:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._log: deque[str] = deque(maxlen=4000)
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_params: dict[str, Any] | None = None
        self._last_exit_code: int | None = None
        self._reader: threading.Thread | None = None

    # ---------- 生命周期 ----------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict[str, Any]:
        proc = self._proc
        return {
            'running': self.running,
            'pid': proc.pid if proc and self.running else None,
            'params': self._last_params,
            'last_exit_code': self._last_exit_code,
            'llm_blocked_exit_code': LLM_BLOCKED_EXIT_CODE,
        }

    def recent_log(self, limit: int = 500) -> list[str]:
        items = list(self._log)
        return items[-limit:]

    # ---------- 日志广播 ----------
    def _emit(self, line: str) -> None:
        self._log.append(line)
        loop = self._loop
        if loop is None:
            return
        for q in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(q.put_nowait, line)
            except RuntimeError:
                pass

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    # ---------- 启动 / 停止 ----------
    def start(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return {'ok': False, 'error': '已有任务在运行'}

            keyword = str(params.get('keyword') or '').strip()
            if not keyword:
                return {'ok': False, 'error': '搜索关键词不能为空'}

            def as_int(key: str, default: int, minimum: int) -> int:
                try:
                    value = int(params.get(key, default))
                except (TypeError, ValueError):
                    value = default
                return max(minimum, value)

            target = as_int('target', 5000, 1)
            max_rounds = as_int('max_rounds', 100, 1)
            round_timeout = as_int('round_timeout', 0, 0)
            max_no_progress = as_int('max_no_progress', 3, 1)
            sleep_seconds = as_int('sleep_seconds', 5, 0)
            page_delay = as_int('page_delay', 12, 0)
            cooldown = as_int('cooldown', 60, 0)
            concurrency = as_int('concurrency', 2, 1)
            mode = 'resume' if params.get('mode') == 'resume' else 'new'

            python_path = str(params.get('python') or sys.executable).strip() or sys.executable

            cmd = [
                python_path, str(paths.SUPERVISOR),
                '--keyword', keyword,
                '--target', str(target),
                '--max-rounds', str(max_rounds),
                '--round-timeout', str(round_timeout),
                '--max-no-progress-rounds', str(max_no_progress),
                '--sleep-seconds', str(sleep_seconds),
                '--page-delay-seconds', str(page_delay),
                '--cooldown-seconds', str(cooldown),
                ('--resume' if mode == 'resume' else '--new-run'),
            ]
            crawl_mode = params.get('crawl_mode')
            if crawl_mode in ('idp_batch', 'generic_per_item'):
                cmd += ['--mode', crawl_mode]

            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUNBUFFERED'] = '1'
            env['BROWSER_USE_DISABLE_INPUT_MONITOR'] = '1'
            env['BROWSER_USE_IMAGE_DOWNLOAD_CONCURRENCY'] = str(concurrency)

            def set_env(env_key: str, param_key: str) -> None:
                value = str(params.get(param_key) or '').strip()
                if value:
                    env[env_key] = value

            set_env('OPENAI_API_KEY', 'api_key')
            set_env('OPENAI_BASE_URL', 'base_url')
            set_env('IDP_CHROME_EXECUTABLE', 'chrome_exe')
            set_env('IDP_CHROME_USER_DATA_DIR', 'chrome_user_data')
            set_env('IDP_CHROME_PROFILE_DIRECTORY', 'chrome_profile')
            set_env('IDP_PROXY_SERVER', 'proxy_server')
            set_env('IDP_SCRAPLING_PYTHON', 'scrapling_py')
            set_env('IDP_CF_URL', 'cf_url')
            set_env('IDP_STORAGE_STATE', 'storage_state')

            try:
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(paths.PROJECT_ROOT),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                    creationflags=creationflags,
                )
            except Exception as exc:  # noqa: BLE001
                self._proc = None
                return {'ok': False, 'error': f'启动失败: {exc}'}

            self._last_params = {
                'keyword': keyword, 'target': target, 'mode': mode,
                'max_rounds': max_rounds, 'concurrency': concurrency,
            }
            self._last_exit_code = None
            self._emit('=' * 70)
            self._emit(f'▶ 启动: {" ".join(cmd)}')
            self._emit('=' * 70)

            self._reader = threading.Thread(target=self._read_output, args=(self._proc,), daemon=True)
            self._reader.start()
            return {'ok': True, 'status': self.status()}

    def _read_output(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._emit(line.rstrip('\n'))
        except Exception as exc:  # noqa: BLE001
            self._emit(f'[读取输出出错] {exc}')
        finally:
            code = proc.wait()
            self._last_exit_code = code
            self._emit('=' * 70)
            self._emit(f'■ 进程已结束，退出码 {code}')
            self._emit('=' * 70)
            self._proc = None
            self._trigger_import()

    def _trigger_import(self) -> None:
        """任务结束后把最新 image_record.jsonl 导入 SQLite,保证前端看到最新数据."""
        try:
            from runner import import_sqlite
            import_sqlite(paths.CACHE_DIR)
            self._emit('[webapp] 已刷新 SQLite 目录数据')
        except Exception as exc:  # noqa: BLE001
            self._emit(f'[webapp] SQLite 刷新跳过: {exc}')

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.running or self._proc is None:
                return {'ok': False, 'error': '当前没有运行中的任务'}
            self._emit('■ 正在停止(终止 supervisor 及其全部子进程)...')
            try:
                terminate_process_tree(self._proc)
            except Exception as exc:  # noqa: BLE001
                return {'ok': False, 'error': f'停止出错: {exc}'}
            return {'ok': True}


manager = RunManager()

