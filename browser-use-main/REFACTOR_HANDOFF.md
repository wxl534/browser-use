# tools_registry 拆分 + 通用化接线 —— 交接文档

> 给接手这项重构的人。读完本文你能：知道**已经做了什么**、**为什么这么做**、
> **下一步具体怎么做**、以及**哪些坑绝对不能踩**。
>
> 仓库根：`browser-use-main/`。所有命令在仓库根执行。
> 运行解释器：`.\.venv\Scripts\python.exe`，并设 `PYTHONIOENCODING=utf-8`。

---

## 0. 一句话背景

`core/tools_registry.py` 原本是 2730 行的 god module（下载/记录/命名/校验/反CF 全挤一起）。
目标：按职责拆成 `views + 多个 service 子模块`，而 `tools_registry.py` 退化为**聚合层**
（re-export 全部对外符号），让所有调用方**零改动**。

另有一项并行任务：把批量工具的硬编码 `IDPAdapter()` 换成 `registry.resolve_adapter`，
兑现「通用化」（见 §6）。本次先做拆分。

---

## 1. 铁律（违反必出 bug，先读这条）

1. **绝不破坏向后兼容**。`worker.py` / `tool_actions/*` / `legacy/*` / `tests/test_worker.py`
   到处 `from tools_registry import X`。拆走任何符号 `X` 后，`tools_registry.py` **必须**继续
   re-export 它。动手前先全仓 grep：
   ```
   grep -rn "from tools_registry import" .   （排除 .venv）
   grep -rn "tools_registry\." .
   ```
   实例：`legacy/site_tools.py:16` 竟从 `tools_registry` import `BaseModel`，所以
   `from pydantic import BaseModel, Field` 这行**不能删**，要当 re-export 留着（已加 `# noqa`）。

2. **运行时路径必须运行时读，不能静态绑定**。`IMAGE_DIR / AGENT_DATA_DIR / RUN_DIR` 会被
   `configure_runtime_paths()` 每轮改写。拆出去的函数里若写
   `from tools_registry import IMAGE_DIR`，会**定格在导入时的旧值**——这正是 WORKFLOW.md §8
   记载过的真实 bug。**正确做法**：用 `runtime_paths.image_dir()` 等 getter，在**调用时**取 live 值。

3. **每拆一步必须跑测试，全绿才继续**：
   ```powershell
   $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m tests.test_worker
   ```
   当前基线 **117/117 通过**（文档里旧写的 101 已过时）。红了立即回退这一步，不要往下叠。

4. **小步前进**。一次只迁一个职责模块，迁完测，绿了再迁下一个。不要一次大挪移。

5. **不 push GitHub**（除非明确被要求）。

---

## 2. 已完成的部分（可直接继续，无需重做）

| 步骤 | 产物 | 状态 |
|---|---|---|
| 抽零副作用层 | `core/registry_views.py` | ✅ 117/117 |
| runtime_paths keystone | `core/runtime_paths.py` | ✅ 117/117 |

### 2.1 `core/registry_views.py`
搬出了**纯数据**：`EXT_TO_PIL_FORMAT` / `GENERAL_IMAGE_METHODS` /
`GENERAL_IMAGE_STRATEGY_LOCK_THRESHOLD` + 7 个 `*Params` Pydantic 模型。
`tools_registry.py` 顶部用 `from registry_views import (...)` re-export。

### 2.2 `core/runtime_paths.py`（keystone，理解它是关键）
- 持有可变状态 `_RUN_DIR / _IMAGE_DIR / _AGENT_DATA_DIR`，import 时从环境变量初始化。
- 对外只给 getter：`run_dir()` / `image_dir()` / `agent_data_dir()` + `set_paths()`。
- `tools_registry.configure_runtime_paths()` 已改为**双写**：既改自己的模块全局
  `IMAGE_DIR` 等（兼容直接读 `tools_registry.IMAGE_DIR` 的测试与未迁移函数），
  **又**调 `runtime_paths.set_paths(...)`。
- **迁移函数时的动作**：把函数体里的 bare `IMAGE_DIR` → `runtime_paths.image_dir()`，
  `AGENT_DATA_DIR` → `runtime_paths.agent_data_dir()`，`RUN_DIR` → `runtime_paths.run_dir()`。
  因为是双写，不必一次改全部 ~60 处引用，只改你当前迁走的那个模块内的引用即可，全程保持绿。
- 注意：`DOWNLOAD_LOCK` 是**永不重赋值**的 `asyncio.Lock()` 单例，静态 import 安全，
  **留在 tools_registry**，不要动它。

---

## 3. 待办：剩余子模块拆分（按依赖顺序）

> 每个模块的函数清单见 §4。顺序不能乱：后者依赖前者。

1. **`core/naming.py`** ← 进行中（尚未落地）
   纯字符串 / 文件名 / hash helper。基本零运行时依赖（除 `_record_image_file_path` 用 `IMAGE_DIR`，
   把它留在 record_store 或改 getter）。**最安全的下一步**。
2. **`core/record_store.py`**（依赖 naming + runtime_paths）
   `image_record.jsonl` 读写 / 去重索引 / 序号分配 / info 表重写 / `_record_saved_image_fast`（核心落库）。
3. **`core/download_methods.py`**（依赖 record_store + naming + runtime_paths）
   三级下载策略链（python_direct → browser_fetch → clean_screenshot）+ 自学习策略 + PIL 去边。
4. **`core/verification.py`**（相对独立）
   反 CF / 人机验证检测与 CDP 点击。
5. **`core/validation.py`**（依赖 record_store）
   `validate_download_artifacts` / `format_download_validation_report` / `_image_hash_groups`。
6. **收尾**：`tools_registry.py` 瘦身为聚合层（目标 < 300 行），只留：
   `tools` 实例、`configure_runtime_paths`、`ALLOWED_BASE_DIRS` / `_is_path_allowed`、
   site-hint 系列（`register_download_site_hint` 等）、`_current_browser_url` / `_navigate_to_image_url`，
   以及把上面所有子模块 `from xxx import *` 全部 re-export。更新模块 docstring。

---

## 4. 函数 → 模块归属表（依据全量 grep，逐个核对再搬）

> 搬迁手法：在新模块写入函数定义 + 必要 import；在 `tools_registry.py` 删掉原定义，
> 改为 `from <module> import (...)` re-export。bare-name 互调（如 naming 内部互相调用）
> 由聚合层 re-export 兜住，但**同一职责的函数尽量一起搬**，减少跨模块 bare-name 依赖。

### naming.py
`_normalize_title` `_safe_download_filename` `_unique_path` `_numbered_file_stem`
`_sequence_from_filename` `_prefix_from_filename` `_renumber_title_if_needed`
`_normalize_border_ratio` `_coerce_int` `_clean_url_text` `_hash_text`
`_normalize_source_url` `_source_hash` `_sha256_file` `_titled_image_stem`
`_final_image_filename` `_rename_image_to_final_name` `normalize_image_ext`
`_pil_format_for_ext` `_image_suffix_from_url` `_image_suffix_from_content_type`
`_content_type_is_json` `_markdown_cell` `_record_sort_key` `_record_sequence`
> 注：`_record_image_file_path`(用 IMAGE_DIR) 归 record_store；`_titled/_final/_rename`
> 这组命名函数无运行时路径依赖，可进 naming。

### record_store.py
`_load_json_list` `_load_jsonl_records` `_load_image_records` `_write_image_records`
`_append_image_record` `_image_record_file` `_record_image_file_path`
`_max_downloaded_record_sequence` `_max_image_file_sequence` `_next_available_image_sequence`
`_safe_requested_image_sequence` `_safe_record_sequence_for_existing_file`
`_safe_requested_image_sequence_from_index` `_build_download_record_index`
`_build_existing_image_hash_index` `_get_cached_download_index` `_record_file_mtime`
`_refresh_generic_index_mtime` `_find_downloaded_record_by_image_url`
`_find_downloaded_record_by_file_hash` `_find_existing_image_file_by_hash`
`_record_file_sha256` `_append_image_info_record` `_rewrite_image_info_file`
`_read_downloaded_records` `_recorded_page_urls` `_existing_recorded_image_urls`
`_record_saved_image_fast`（核心）`DownloadRecordIndex`（dataclass，含 add_record）
> `DownloadRecordIndex.add_record` 调 `_record_sequence`/`_record_file_sha256`，
> 故它跟 record_store 一起搬最自然。

### download_methods.py
`_download_file` `_download_image_to_file` `_browser_fetch_image_to_file`
`_write_image_bytes_to_file` `_save_clean_visible_image_screenshot`
`_trim_plain_border_from_image` `_get_visible_image_rect` `_save_pil_image`
`_validate_saved_image_file` `_get_browser_cookie_header`
`_resolve_generic_image_url` `_extract_generic_image_candidates`
`_ordered_generic_image_methods` `_record_generic_image_method_success`
`_record_generic_image_method_failure` `_save_generic_image_by_method`
`_load_generic_image_strategy` `_write_generic_image_strategy`
`_generic_image_strategy_file`
`_safe_requested_image_filename` `_safe_requested_image_filename_from_type`
`_safe_png_filename` `_safe_image_filename_with_ext`
> IIIF 解析 helper（`_looks_like_iiif_*` / `_collect_iiif_manifest_image_urls` /
> `_iiif_*` / `_resolve_iiif_manifest_to_image_url` / `_fetch_json_url`）可单列
> `iiif_resolve.py`，或并入 download_methods。评估与 `adapters/iiif.py` 是否重复。

### verification.py
`_detect_human_verification` `_collect_verification_click_targets`
`_verification_click_points` `_cdp_click_point` `_attempt_cloudflare_autoclick`
`_get_visible_image_rect`（若被 screenshot 复用，留 download_methods）

### validation.py
`validate_download_artifacts` `format_download_validation_report` `_image_hash_groups`

### 留在 tools_registry.py（聚合层）
`tools` / `registry` / `configure_runtime_paths` / `ALLOWED_BASE_DIRS` / `_is_path_allowed`
/ `LEGACY_TOOLS_ENABLED` / `legacy_tools_action` / `register_download_site_hint` 及
`_matching_site_hints` / `_site_*` 系列 / `_current_browser_url` / `_navigate_to_image_url`
/ `_search_item_*` 游标 / `_select_next_search_item` / `_enumerate_current_page_items`
/ `_format_output` + 各 `@tools.action` 注册 + 全部子模块 re-export。

---

## 5. 标准操作流程（每个模块照做）

1. `grep -n "^def \|^async def \|^class " core/tools_registry.py` 定位目标函数当前行号
   （**每次编辑后行号会变，务必重新 grep**）。
2. 新建 `core/<module>.py`：写 docstring + import（`from __future__ import annotations`，
   需要路径就 `import runtime_paths`，需要 naming 就 `from naming import ...`）。
3. 把目标函数**原样**剪过去；函数体内 bare 运行时路径改 getter（见 §2.2）。
4. 在 `tools_registry.py` 删除原定义，改成 `from <module> import (列出全部搬走的名字)  # noqa: F401`。
5. 跑 `python -m tests.test_worker`。绿 → 提交心智上的「这一步完成」；红 → 看 traceback，
   90% 是漏 re-export 或静态绑定了路径，修掉或回退。
6. 冒烟：`python -c "import sys; sys.path.insert(0,'core'); sys.path.insert(0,'scripts'); import worker; import tools_registry; print('ok')"`。

---

## 6. 并行任务：通用化接线（拆分稳定后再做）

现状：`tool_actions/download_current_idp_search_page_images.py:22` 与
`tool_actions/navigate_idp_search_page.py:53` 硬编码 `IDPAdapter()`，而
`adapters/registry.py:resolve_adapter_for_session()` 早已实现却无人调用，
`adapters/site_profiles/` **只有 README，无真实 profile**——所以「通用」从未端到端验证。

要做：
1. 两个 tool_actions 把 `IDPAdapter()` 换成 `await resolve_adapter_for_session(browser_session)`
   （resolve 内置兜底仍是 IDP，零回归）。
2. `navigate_idp_search_page._guard_idp_page` 去 IDP 耦合：进度文件名改用
   `adapter.progress_file_name`（守卫逻辑本身站点无关）。
3. `adapters/registry.py` 加 profile 缓存（按 mtime），避免每次 listdir + 重建。
4. 选 1 个真实第二站（候选 Bodleian / Gallica，**先实测 manifest 能否确定性拼接**），
   用 `adapters/profile_site.py` 生成 profile 草稿 → 人工补全 `manifest_template` 的 `{id}`
   → 存 `adapters/site_profiles/<id>.json` → 端到端跑通 ≥1 页并核对 `image_record.jsonl`。
5. 补测试：`resolve_adapter` 命中表 + 用 pytest-httpserver mock 最小 IIIF manifest 跑
   ConfigIIIFAdapter 的 extract→download。
6. 更新 WORKFLOW.md §7/§11 状态：「待接线」→「已接线」。

验收：IDP 行为零回归 + 第二站零 Python 代码仅 JSON 跑通 + resolve 单测覆盖三种命中。

---

## 7. 进度追踪

会话内用 SQLite `todos` 表跟踪（id: views/runtime-paths/naming/record-store/
download-methods/verification/validation/finalize）。当前：views、runtime-paths 已 done，
naming 进行中。接手后请同步更新状态。

## 8. 验收总标准
- [ ] `python -m tests.test_worker` 全程 117/117。
- [ ] worker / 3 个 tool_actions / batch_download / adapters.registry / legacy 的 import 零改动可用。
- [ ] `tools_registry.py` < 300 行，纯聚合层；各子模块单一职责。
- [ ] 一轮实跑（或冒烟）落库目录正确，运行时路径切换行为不变。
