"""运行时产物路径的单一可信源(可变状态 + getter).

为什么需要它:``configure_runtime_paths`` 每轮把图片/数据目录切到当轮的
ImagesCache.原先这三个路径是 ``tools_registry`` 的模块全局,函数靠 bare-name
读取(即运行时读 live 值).一旦把这些函数拆到别的模块,就**不能**再
``from tools_registry import IMAGE_DIR``(那是导入时静态绑定,会定格在旧值——
正是 WORKFLOW §8 记载过的 bug).

解决:把可变状态收拢到本模块,任何子模块都通过 ``runtime_paths.image_dir()``
等 getter **在调用时**读 live 值.``tools_registry`` 仍保留同名模块属性并在
``configure_runtime_paths`` 里双写,以兼容直接读 ``tools_registry.IMAGE_DIR``
的现有测试与尚未迁移的函数.

本模块与 tools_registry 同在 core/,``Path(__file__).parent.parent`` 推导出的
默认路径与原逻辑一致.
"""
from __future__ import annotations

import os
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent.parent


def _env_path(var: str, default: Path) -> Path:
    return Path(os.environ.get(var, str(default))).resolve()


# 导入时从环境变量初始化,与 tools_registry 原先的默认逻辑保持一致.
_RUN_DIR = _env_path('BROWSER_USE_RUN_DIR', _PROJECT_DIR)
_IMAGE_DIR = _env_path('BROWSER_USE_IMAGE_DIR', _PROJECT_DIR / 'image')
_AGENT_DATA_DIR = _env_path('BROWSER_USE_AGENT_DATA_DIR', _PROJECT_DIR / 'browseruse_agent_data')


def run_dir() -> Path:
    return _RUN_DIR


def image_dir() -> Path:
    return _IMAGE_DIR


def agent_data_dir() -> Path:
    return _AGENT_DATA_DIR


def set_paths(run: Path, image: Path, data: Path) -> None:
    """切换本轮运行时路径(由 ``configure_runtime_paths`` 调用)."""
    global _RUN_DIR, _IMAGE_DIR, _AGENT_DATA_DIR
    _RUN_DIR = Path(run).resolve()
    _IMAGE_DIR = Path(image).resolve()
    _AGENT_DATA_DIR = Path(data).resolve()
