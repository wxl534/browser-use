"""集中解析 webapp 后端需要的所有路径与对外部脚本的依赖。

后端不复制业务逻辑，只读取爬虫产物（image_catalog.sqlite3 / ImagesCache /
image_record.jsonl / task.md / *_report.md / idp_progress.json）并复用
auto_run_until_target / configure_resume_target 里的轻量标准库函数。

所有路径都相对 `browser-use-main/` 推导，禁止硬编码绝对地址。
"""
from __future__ import annotations

import sys
from pathlib import Path

# webapp/backend/paths.py -> browser-use-main/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 让后端可以 import 爬虫根目录下的脚本（均为标准库实现，导入不会拉起 browser_use）。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CACHE_DIR = PROJECT_ROOT / 'Images' / 'ImagesCache'
SQLITE_FILE = CACHE_DIR / 'image_catalog.sqlite3'
RECORD_FILE = CACHE_DIR / 'image_record.jsonl'
INFO_FILE = CACHE_DIR / 'temple_photo_info.md'
PROGRESS_FILE = CACHE_DIR / 'idp_progress.json'
FINAL_REPORT_FILE = CACHE_DIR / 'final_download_report.md'
TASK_FILE = PROJECT_ROOT / 'task.md'
TASK_CONFIG_FILE = PROJECT_ROOT / 'task_config.json'
SUPERVISOR = PROJECT_ROOT / 'auto_run_until_target.py'

# 前端构建产物（vite build 后由 FastAPI 静态托管）。
FRONTEND_DIST = Path(__file__).resolve().parents[1] / 'frontend' / 'dist'


def cache_dir() -> Path:
    return CACHE_DIR


def sqlite_file() -> Path:
    return SQLITE_FILE
