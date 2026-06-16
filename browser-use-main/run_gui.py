"""
本地桌面前端（零依赖，纯 tkinter）：在一个窗口里填写必要信息后启动
auto_run_until_target.py 这个外部 supervisor，实时显示日志与进度，并可一键停止。

设计原则：本前端只做“参数填写 + 启动 + 实时进度/日志 + 停止”这层，所有业务逻辑
（归档旧缓存、改写 task.md、断点续跑等）仍由 supervisor 通过 --keyword/--target/
--resume/--new-run 等参数完成，避免在前端重复实现而产生分叉。

运行：用装有 browser-use 依赖的那个 Python 启动本文件，例如
    python run_gui.py
窗口里的“Python 解释器”默认就是当前解释器（sys.executable），子进程会用它来跑。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# 复用 supervisor / 配置脚本里的轻量函数（均为标准库实现，导入不会拉起 browser_use）。
from auto_run_until_target import (
    LLM_BLOCKED_EXIT_CODE,
    detect_search_keyword,
    read_downloaded_count,
    read_target_from_task,
    terminate_process_tree,
)
from configure_resume_target import DEFAULT_CACHE_DIR, TASK_FILE

BASE_DIR = Path(__file__).resolve().parent
SUPERVISOR = BASE_DIR / 'auto_run_until_target.py'
DEFAULT_BASE_URL = 'https://openapi.seu.edu.cn/v1'

EXIT_MARKER = '__EXIT__'


def _prefill_keyword() -> str:
    try:
        if TASK_FILE.exists():
            return detect_search_keyword(TASK_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return ''


def _prefill_target() -> str:
    try:
        return str(read_target_from_task())
    except Exception:
        return '5000'


class RunnerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.log_queue: queue.Queue = queue.Queue()
        self.cache_dir = DEFAULT_CACHE_DIR

        root.title('IDP 爬虫 · 本地启动器')
        root.geometry('820x640')
        root.minsize(720, 560)

        self._build_form()
        self._build_console()

        # 周期性把后台线程的输出刷到界面，并轮询进度。
        self.root.after(120, self._drain_log)
        self.root.after(1500, self._poll_progress)

    # ---------- UI 构建 ----------
    def _build_form(self) -> None:
        frm = ttk.LabelFrame(self.root, text='运行参数')
        frm.pack(fill='x', padx=10, pady=(10, 6))
        for col in range(4):
            frm.columnconfigure(col, weight=1 if col in (1, 3) else 0)

        self.var_keyword = tk.StringVar(value=_prefill_keyword())
        self.var_target = tk.StringVar(value=_prefill_target())
        self.var_mode = tk.StringVar(value='new')
        self.var_max_rounds = tk.StringVar(value='100')
        self.var_round_timeout = tk.StringVar(value='0')
        self.var_max_no_progress = tk.StringVar(value='3')
        self.var_sleep = tk.StringVar(value='5')
        self.var_page_delay = tk.StringVar(value='0')
        self.var_cooldown = tk.StringVar(value='60')
        self.var_concurrency = tk.StringVar(value='3')
        self.var_api_key = tk.StringVar(value=os.environ.get('OPENAI_API_KEY', ''))
        self.var_base_url = tk.StringVar(value=os.environ.get('OPENAI_BASE_URL', DEFAULT_BASE_URL))
        self.var_python = tk.StringVar(value=sys.executable)

        def row(r: int, label: str, var: tk.StringVar, *, show: str | None = None, col: int = 0, width: int = 24):
            ttk.Label(frm, text=label).grid(row=r, column=col, sticky='w', padx=6, pady=4)
            ent = ttk.Entry(frm, textvariable=var, width=width, show=show)
            ent.grid(row=r, column=col + 1, sticky='ew', padx=6, pady=4)
            return ent

        row(0, '搜索关键词', self.var_keyword, col=0)
        row(0, '目标数量', self.var_target, col=2, width=12)

        ttk.Label(frm, text='运行模式').grid(row=1, column=0, sticky='w', padx=6, pady=4)
        mode_box = ttk.Frame(frm)
        mode_box.grid(row=1, column=1, sticky='w', padx=6, pady=4)
        ttk.Radiobutton(mode_box, text='从头开始', variable=self.var_mode, value='new').pack(side='left')
        ttk.Radiobutton(mode_box, text='断点续跑', variable=self.var_mode, value='resume').pack(side='left', padx=(10, 0))

        row(1, '最大轮数', self.var_max_rounds, col=2, width=12)
        row(2, '单轮超时(秒,0不限)', self.var_round_timeout, col=0, width=12)
        row(2, '连续无进展上限', self.var_max_no_progress, col=2, width=12)
        row(3, '轮间隔(秒)', self.var_sleep, col=0, width=12)
        row(3, '每页节流(秒)', self.var_page_delay, col=2, width=12)
        row(4, '限流冷却基数(秒)', self.var_cooldown, col=0, width=12)
        row(4, '图片并发数', self.var_concurrency, col=2, width=12)

        row(5, 'OPENAI_API_KEY', self.var_api_key, show='*', col=0, width=42)
        row(6, 'OPENAI_BASE_URL', self.var_base_url, col=0, width=42)
        row(7, 'Python 解释器', self.var_python, col=0, width=42)

        btns = ttk.Frame(self.root)
        btns.pack(fill='x', padx=10, pady=(0, 4))
        self.btn_start = ttk.Button(btns, text='▶ 开始运行', command=self.on_start)
        self.btn_start.pack(side='left')
        self.btn_stop = ttk.Button(btns, text='■ 停止', command=self.on_stop, state='disabled')
        self.btn_stop.pack(side='left', padx=(8, 0))
        ttk.Button(btns, text='清空日志', command=self._clear_log).pack(side='left', padx=(8, 0))

        self.var_status = tk.StringVar(value='状态：空闲    进度：- / -')
        ttk.Label(self.root, textvariable=self.var_status, anchor='w').pack(fill='x', padx=12, pady=(0, 4))

    def _build_console(self) -> None:
        frm = ttk.LabelFrame(self.root, text='运行日志')
        frm.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.console = tk.Text(frm, wrap='word', state='disabled', height=18,
                               bg='#1e1e1e', fg='#d4d4d4', insertbackground='#d4d4d4')
        self.console.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(frm, command=self.console.yview)
        sb.pack(side='right', fill='y')
        self.console.configure(yscrollcommand=sb.set)

    # ---------- 日志 ----------
    def _append(self, text: str) -> None:
        self.console.configure(state='normal')
        self.console.insert('end', text + '\n')
        self.console.see('end')
        self.console.configure(state='disabled')

    def _clear_log(self) -> None:
        self.console.configure(state='normal')
        self.console.delete('1.0', 'end')
        self.console.configure(state='disabled')

    def _drain_log(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item and item[0] == EXIT_MARKER:
                    self._on_finished(item[1])
                else:
                    self._append(str(item))
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    # ---------- 进度 ----------
    def _poll_progress(self) -> None:
        running = self.proc is not None
        try:
            count = read_downloaded_count(self.cache_dir)
        except Exception:
            count = '-'
        try:
            target = read_target_from_task()
        except Exception:
            target = '-'
        state = '运行中' if running else '空闲'
        self.var_status.set(f'状态：{state}    进度：{count} / {target}')
        self.root.after(1500, self._poll_progress)

    # ---------- 启动 / 停止 ----------
    def _validate(self) -> dict | None:
        keyword = self.var_keyword.get().strip()
        if not keyword:
            messagebox.showerror('参数错误', '搜索关键词不能为空。')
            return None

        def as_int(var: tk.StringVar, name: str, minimum: int) -> int | None:
            try:
                value = int(var.get().strip())
            except ValueError:
                messagebox.showerror('参数错误', f'{name} 必须是整数。')
                return None
            if value < minimum:
                messagebox.showerror('参数错误', f'{name} 不能小于 {minimum}。')
                return None
            return value

        target = as_int(self.var_target, '目标数量', 1)
        max_rounds = as_int(self.var_max_rounds, '最大轮数', 1)
        round_timeout = as_int(self.var_round_timeout, '单轮超时', 0)
        max_no_progress = as_int(self.var_max_no_progress, '连续无进展上限', 1)
        sleep_seconds = as_int(self.var_sleep, '轮间隔', 0)
        page_delay = as_int(self.var_page_delay, '每页节流', 0)
        cooldown = as_int(self.var_cooldown, '限流冷却基数', 0)
        concurrency = as_int(self.var_concurrency, '图片并发数', 1)
        if None in (target, max_rounds, round_timeout, max_no_progress, sleep_seconds, page_delay, cooldown, concurrency):
            return None

        python_path = self.var_python.get().strip() or sys.executable
        if not Path(python_path).exists():
            messagebox.showerror('参数错误', f'找不到 Python 解释器：{python_path}')
            return None

        return {
            'keyword': keyword,
            'target': target,
            'max_rounds': max_rounds,
            'round_timeout': round_timeout,
            'max_no_progress': max_no_progress,
            'sleep_seconds': sleep_seconds,
            'page_delay': page_delay,
            'cooldown': cooldown,
            'concurrency': concurrency,
            'python': python_path,
        }

    def on_start(self) -> None:
        if self.proc is not None:
            return
        cfg = self._validate()
        if cfg is None:
            return

        cmd = [
            cfg['python'], str(SUPERVISOR),
            '--keyword', cfg['keyword'],
            '--target', str(cfg['target']),
            '--max-rounds', str(cfg['max_rounds']),
            '--round-timeout', str(cfg['round_timeout']),
            '--max-no-progress-rounds', str(cfg['max_no_progress']),
            '--sleep-seconds', str(cfg['sleep_seconds']),
            '--page-delay-seconds', str(cfg['page_delay']),
            '--cooldown-seconds', str(cfg['cooldown']),
            ('--resume' if self.var_mode.get() == 'resume' else '--new-run'),
        ]

        env = os.environ.copy()
        api_key = self.var_api_key.get().strip()
        base_url = self.var_base_url.get().strip()
        if api_key:
            env['OPENAI_API_KEY'] = api_key
        if base_url:
            env['OPENAI_BASE_URL'] = base_url
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        # supervisor 由前端托管，子进程不再自行监听 stdin。
        env['BROWSER_USE_DISABLE_INPUT_MONITOR'] = '1'
        env['BROWSER_USE_IMAGE_DOWNLOAD_CONCURRENCY'] = str(cfg['concurrency'])

        try:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
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
        except Exception as exc:
            messagebox.showerror('启动失败', str(exc))
            self.proc = None
            return

        self._append('=' * 70)
        self._append(f'▶ 启动: {" ".join(cmd)}')
        self._append('=' * 70)
        self.btn_start.configure(state='disabled')
        self.btn_stop.configure(state='normal')

        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log_queue.put(line.rstrip('\n'))
        except Exception as exc:
            self.log_queue.put(f'[读取输出出错] {exc}')
        finally:
            code = proc.wait()
            self.log_queue.put((EXIT_MARKER, code))

    def on_stop(self) -> None:
        if self.proc is None:
            return
        self._append('■ 正在停止（终止 supervisor 及其全部子进程）...')
        try:
            terminate_process_tree(self.proc)
        except Exception as exc:
            self._append(f'[停止出错] {exc}')

    def _on_finished(self, code: int) -> None:
        self.proc = None
        self.btn_start.configure(state='normal')
        self.btn_stop.configure(state='disabled')
        self._append('=' * 70)
        self._append(f'■ 进程已结束，退出码 {code}')
        self._append('=' * 70)
        if code == LLM_BLOCKED_EXIT_CODE:
            messagebox.showwarning(
                'LLM 端点被拦截',
                '子进程报告 LLM 端点被网关/门户拦截（退出码 3）。\n'
                '请确认已连接校园网/VPN，且 OPENAI_API_KEY / OPENAI_BASE_URL 正确后重试。',
            )
        elif code == 0:
            messagebox.showinfo('完成', '已达到目标数量，任务完成。')

    def on_close(self) -> None:
        if self.proc is not None:
            if not messagebox.askyesno('确认退出', '任务仍在运行，退出将停止它。确定吗？'):
                return
            try:
                terminate_process_tree(self.proc)
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = RunnerGUI(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()


if __name__ == '__main__':
    main()
