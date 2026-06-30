# 相似网站通用图片爬虫 — 工作流程与代码文件详解

> **项目目标:做到对"结构相似的图库/IIIF 站点"通用的自动爬虫,而非仅 idp.bl.uk 单站。**
> 当前为**未完成版**:已落地通用骨架(SiteAdapter 契约 + 配置驱动 ConfigIIIFAdapter + DOM profiler),
> 但仍以 IDP 作为已跑通的首个站点(MVP),通用自动接入链路尚未全部接线,需后续完善。
> 任务流程:搜索关键词 → 批量提取藏品 → 解析 IIIF manifest → 并发下图 → 去重落库,直到达成目标张数。
> 设计核心:**LLM 每页只决策一次「调批量工具」,其余全部确定性代码**,配合方案C 反 Cloudflare 通行证实现长时间稳定运行。

> ⚠️ 现状说明:IDP 是当前唯一接线的站点;`adapters/generic_config.py`、`registry.py`、`profile_site.py`
> 已实现"零代码接新站"的通用路径,但批量工具暂时硬编码 IDPAdapter,通用 resolver 未启用——
> 这是有意为之,先用 IDP 验证"反CF + 快速批量"长稳跑通,再切换到通用自动接入。

---

## 0. 一图看懂整体架构

```
runner.py  (监工 / 多轮调度 + 方案C 刷新 cf_clearance)
        │  每轮 fork 子进程
        ▼
   worker.py  (单轮入口: 读 task.md → 建浏览器 → 跑 Agent)
        │  调一次/页
        ▼
 tool_actions/download_current_idp_search_page_images.py  (批量工具入口)
        │
        ▼
 core/batch_download.py : run_search_page_batch  (3 阶段编排器)
        ├─ adapters/idp.py   : 扫页拿 item + manifest URL (纯 JS)
        ├─ adapters/iiif.py  : fetch manifest → 抽图打分
        ├─ core/concurrent_download.py : 并发下字节
        └─ core/idp_page_progress.py : 写进度，供续跑
```

反 CF 取证旁路：
```
scripts/fetch_cf_cookie.py (Scrapling 隔离环境) → cf_storage.json → worker.py 注入 storage_state
```

---

## 1. 任务定义层：task.md / task_config.json / 模板

- **`task_config.json`** — 唯一可手改的配置：`keyword`、`target_count`、`site_url`、`allowed_hosts`、`mode`(=`idp_batch`)、`force_generic`。
- **`scripts/render_task.py`** — 把 `task_config.json` + `task_template.md` 渲染成 `task.md`。命令：`python scripts/render_task.py --mode idp_batch`。
- **`task.md`** — 自动生成、给 Agent 读的指令书；**请勿手改**。规定「优先批量工具、按进度文件递增翻页、禁止手动 fallback、人机验证交给监工刷新」。

> 改搜索词/目标/站点 = 改 `task_config.json` → 重新渲染，绝不直接编辑 task.md。

---

## 2. 监工层：`runner.py`（旧名 auto_run_until_target.py）

负责「跑到目标为止」的外层循环，每轮启动一个全新的 `worker.py` 子进程（隔离崩溃/内存泄漏）。

| 关键函数 | 行 | 作用 |
|---|---|---|
| `auto_run_until_target` | 666 | 主循环：读目标→预取通行证→跑一轮→统计新增→决定是否续 |
| `ensure_cf_cookie` | 627 | 调 `scripts/fetch_cf_cookie.py` 取/刷新 cf_clearance |
| `_cf_cookie_fresh` | 609 | 看 `cf_storage.json` 过期时间决定是否刷新 |
| `run_main_subprocess` | 491 | 用 `BROWSER_USE_RUN_DIR` 指定缓存目录跑 worker.py |
| `configure_before_run` | 333 | 交互/参数设关键词、目标、站点 |

方案C 关键逻辑（782 行附近）：某轮 0 新增 → 判定 cf_clearance 失效 → `ensure_cf_cookie(force=True)` 重取 → 下轮带新通行证重启。

---

## 3. 反 CF 取证：`scripts/fetch_cf_cookie.py`

在独立 Scrapling 解释器里用 `StealthyFetcher`（patchright + browserforge 指纹）自动过 Turnstile，导出 `cf_clearance` 等 cookie 为 browser-use 的 `storage_state` JSON（`cf_storage.json`）。

- `cf_clearance` **绑出口 IP + 轻度绑 UA**：取证与主程序必须同出口 IP；UA 写入 `_meta` 供注入对齐。
- 过期 30 分钟~数小时；退出码 0=拿到、2=无（IP 信誉差需住宅代理）。

### 3.1 实测复盘：谁才是真正过 CF 的功臣（2026-06-30 5000 张长跑）

一次 5000 张 `china buddhist` 的完整跑（`info.log`，03:17→08:49，5000/5000 达标）实证了反CF 各路径的实际分工：

- **预注入 `cf_clearance`（主路径）才是真正建功的**。运行中途偶发再弹 CF 时，靠的是这张预注入通行证在等待窗口内生效 / 页面 settle 后放行。Agent 自身 Memory 也写明 *"bypassed automatically (cf_clearance cookie was pre-injected)"*。
- **`wait_for_human_verification` 的 `auto_click` 兜底被触发但未建功**。从 `tool_actions/wait_for_human_verification.py:32-47` 逻辑反推：若 `_attempt_cloudflare_autoclick` 成功会**立即**返回"已自动点击通过"；而实测该调用**耗满整个 30s timeout**（03:19:00→03:19:30），证明 `auto_clicked=False`，代码跌进第 49 行被动轮询分支，靠等通行证生效而非 CDP 点 Turnstile 复选框过关。
- **结论与既有认知一致**：Turnstile 按后台指纹 + IP 信誉判定，CDP/视觉自动点击无法绕过。所以 `auto_click` 只是"单击放行"型的偶发兜底，**不可依赖**；长稳的根基始终是 Scrapling 预注入通行证 + 同出口 IP（必要时住宅代理）。
- **排查提示**：该工具内部不打 `[tools]` 日志，只通过 `ActionResult` 返回文本；故日志里查不到 auto_click 细节属正常，**不能据此判断它没运行**。判断是否真跑，看调用前后的 wall-clock 间隔——耗满 timeout = 走了被动轮询、auto_click 没成。

---

## 4. 单轮入口：`worker.py`（旧名 main.py）

| 函数 | 行 | 作用 |
|---|---|---|
| `build_browser` | 79 | 注入 cf_clearance(storage_state) + 对齐 UA + 可选真实 Chrome profile/代理 |
| `extract_target_image_count` / `extract_search_keyword` | core/task_parse.py | 从 task.md 读目标与关键词 |
| `select_active_cache_dir` | 180 | 选/归档本轮 ImagesCache |
| `sync_idp_progress_from_page_queue` | 407 | 从 page_progress 同步续跑页 |
| `build_resume_task_context` | 444 | 续跑时把断点上下文拼进任务 |
| `run_idp_resume_preflight` | 519 | 续跑前先翻到正确页 |
| `preflight_llm_check` | 794 | 先验 LLM 端点可达，避免空烧 token |
| `run_agent_once` | 814 | 组装 Agent(LLM+Browser+Tools) 跑一轮 |

> 行号为重组后近似值,具体以代码为准;部分辅助函数已拆到 `core/task_parse.py`、`core/cache_layout.py`。

---

## 5. 批量工具入口：`tool_actions/download_current_idp_search_page_images.py`

注册为 Agent 工具，硬编码 `IDPAdapter()`，转手 `run_search_page_batch`。这是 Agent 每页唯一调用——一次 = 整页几十张图。

---

## 6. 核心编排器：`core/batch_download.py : run_search_page_batch` (245)

| 阶段 | 行 | 说明 |
|---|---|---|
| 准备 | 285-307 | 读 image_record 算已下载/去重索引/下一序号；节流等待 |
| 扫页 | 313 | `adapter.extract_items` 整页提取 item（纯 JS） |
| 翻页判定 | 342-431 | 本页消费完=正常翻页；真空页=报错并写 tag |
| Phase1 解析 | 443-489 | 逐 item 解析 manifest→图 URL，过滤白名单 + 已下载 |
| Phase2 下载 | 491-527 | `ConcurrentImageDownloader` + `asyncio.gather` 并发拉字节，失败回退浏览器 fetch |
| Phase3 落库 | 529-589 | `DOWNLOAD_LOCK` 内 sha256 去重、分配序号、写 jsonl |
| 进度报告 | 591-660 | 算 next_page/next_index，写 idp_progress.json |

---

## 7. 站点差异层:adapters/ ——通用化的核心

设计意图:把"站点差异"收敛到 `SiteAdapter`,通用编排器对任何同结构站点零改动。接入新站只需 3 个未知量:item 链接选择器、item-id 正则、manifest URL 模板。

- **`base.py`** — `SiteAdapter` 抽象契约(URL 模板、extract_items、resolve_item_image_urls)。
- **`iiif.py`** — `IIIFAdapter` 基类:浏览器 fetch manifest JSON、遍历收图、打分降权缩略图/logo。**所有 IIIF 站点共用**,是通用化的关键(IDP/Gallica/梵蒂冈/Bodleian 等都暴露 IIIF)。
- **`idp.py`** — IDP 具体实现(首个 MVP):`extract_items`(140) 扫页+正则抠 ID+拼 manifest;`manifest_url_for_item`(175)。
- **`generic_config.py`** — `ConfigIIIFAdapter`:读 JSON profile 即可跑批量,**零站点代码**,通用接新站的目标形态。
- **`profile_site.py`** — 自动 profiler:读结果页 DOM 启发式推断 3 个未知量,生成 profile 草稿(人工确认)。
- **`registry.py`** — 按 URL 自动选 adapter(IDP/各 profile/兜底)。
- `site_profiles/` — 每站一个 JSON,无需写 Python。

**未完成/路线图**:上述通用三件套已实现但**未接线**——批量工具暂硬编码 IDPAdapter。后续切换 = 工具改用 `registry.resolve_adapter` + 跑 profiler 生成 profile,即可在结构相似站点零代码接入。DOM 差异大或 manifest 非确定性的站仍需人工补 profile 或手写 adapter。

## 8. 站点插件 & 下载基建
- **`sites/idp.py`** — 通过 `register_download_site_hint` 注入逐 item 兜底工具的 IDP 提示,核心 registry 无硬编码。
- **`core/concurrent_download.py`** — 共享 aiohttp 会话 + Semaphore 并发池（默认 4，`BROWSER_USE_IMAGE_DOWNLOAD_CONCURRENCY` 可调）。
- **`core/idp_page_progress.py`** — `mark_page_batch_result` 维护 page 队列 + next_page/next_index；`reconcile_frontier_from_records` 续跑自愈:用 `image_record.jsonl` 每条记录的 `source_page` 重建页级 frontier(`< frontier` 的页标 `done`,frontier 页留 `in_progress` 让续跑去重补尾,**`[1,frontier)` 内无记录的空洞页补 `pending` 回头补扫**杜绝中间页全重复被永久跳过),**即使页队列丢失/落后也能从图片级事实来源恢复真实进度**,杜绝从低页逐页重走已下载页(0 新增 → LLM 跳深页的根因)。
- **每条图片记录新增 `source_page` 字段**(`core/batch_download.py` 盖上当前搜索页码 → `core/tools_registry.py` `_record_saved_image_fast` 落库),是 frontier 重建的依据。续跑入口 `worker.sync_idp_progress_from_page_queue` / `runner.sync_progress_from_page_queue` 在 `select_next_page` 前先调用 reconcile。
- **`tool_actions/navigate_idp_search_page.py`** — 翻页工具内置**防跳页守卫** `_guard_idp_page`：LLM 传入的 `page` 会被夹到 `[1, min(current_page+1, 200)]`，杜绝「page 8 → page 500」式跳页（current_page 取自 `idp_progress.json`，续跑时由 `select_next_page` 写入，故深页续跑仍可顺序起步）。某页 0 新增由确定性队列自动 +1 推进，不需要、也不允许 agent 手动跳深页。
- **`sites/idp.py`** — `_idp_progress_file()` 改用**实时** `AGENT_DATA_DIR`（修复导入时静态捕获导致的脱节 bug：此前工具的 `idp_progress.json` 落到 repo 默认 `browseruse_agent_data/`，与 worker 写的 `run_dir/ImagesCache/idp_progress.json` 不是同一文件，跨 run 残留还会让守卫失效）。
- **`core/tools_registry.py`** — 全部工具注册、记录/去重/校验/finish 等共享基建。

---

## 8.5 下载工具选择 / 信息提取 / 重命名 / 存储位置（重构重点）

> 这部分是你要重构的核心,逐项拆开说明现状与代码位置。

### A. 已注册的下载相关工具（tool_actions/）

| 工具 | 用途 | 调用频率 |
|---|---|---|
| `download_current_idp_search_page_images` | **主力**:整页批量,零 LLM | 每页 1 次 |
| `navigate_idp_search_page` / `finish_download_task` / `validate_download_completion` / `wait_for_human_verification` | 翻页/收尾/校验/过验证 | 按需 |

> 逐个 item 兜底工具（`download_image_from_url` / `next_search_item` / `extract_page_to_markdown`）实测效果差、从未真正使用，已迁出到 `legacy/` 且默认不注册；`record_downloaded_image` 保留原位但改由 `BROWSER_USE_ENABLE_LEGACY_TOOLS` 控制，默认不进 agent 工具目录。旧站工具(LOC/Kyohaku)同样默认不注册。

### B. 下载方法选择逻辑（多策略 + 自学习）

`download_image_from_url`（现位于 `legacy/`，默认不注册）按 `_ordered_generic_image_methods`(189)排序尝试,逐个 fallback:
1. `python_direct` — `_download_image_to_file`(1444):aiohttp 直连,校验 Content-Type=image。
2. `browser_context_fetch` — `_browser_fetch_image_to_file`(1510):浏览器内 fetch,带 session cookie/referer,过 referer 限制。
3. `clean_screenshot` — `_save_clean_visible_image_screenshot`(1677):裁掉纯色边后截图兜底(`allow_clean_screenshot`)。
- IIIF/详情页 URL 先经 `_resolve_iiif_manifest_to_image_url` 解析成真直链。
- **自学习**:`_record_generic_image_method_success` 连胜后锁定优先方法,减少试错。
- 批量工具走更简版:仅 `python_direct`→`browser_context_fetch`(batch_download 504),无截图。

### C. 图片信息提取逻辑

- 批量:`adapters/iiif.py` fetch manifest → `label`/`summary`/`metadata`(metadata 数组拼成 `k: v;`)。
- **详情页 Overview 补充(零 LLM,确定性,跨站通用)**:IIIF manifest 的 metadata 在不同站点丰俭不一(IDP 只放 Pressmark/Description/Reading Direction 3 个字段,详情页却有 Date/Find site/Measurement/Language/Subject/Institution/Provenance)。为不丢字段,`SiteAdapter` 新增可选钩子 `resolve_item_detail_overview`(base 默认返回 `{}`,零开销)。
  - **通用引擎** `adapters/detail_overview.py`:配置驱动,支持 3 种最常见的画廊站详情 DOM 模式——`sections`(容器+标签+值,如 IDP 的 `.detaildropdown__section`)、`dl`(`<dt>/<dd>`)、`table`(`<th>/<td>`),外加 `header_fields`(页眉零散字段)。在浏览器同源 fetch 详情页(自动带 cf_clearance cookie 过 CF),`DOMParser` 确定性解析。**SSR HTML,字段真实,绝不编造**;任何失败优雅降级 `{}`,不影响下载。
  - **IDP 是该引擎的一个 profile 实例**(`adapters/idp.py` 的 `_IDP_DETAIL_OVERVIEW_CONFIG`:sections 模式 + `.detaildropdown__section`/`h4` + 页眉 Pressmark/Material)。
  - **任意 IIIF 站点**用 `ConfigIIIFAdapter`(`adapters/generic_config.py`)时,只需在 profile 加 `detail_overview` 段(mode/section_selector/label_selector/value_selector/header_fields)+ 可选 `detail_url_template` 即可零代码接入;不配则只用 manifest 元数据。
  - 通用 helper `evaluate_js_in_browser`(`adapters/iiif.py`)封装 CDP `Runtime.evaluate`。
- **合并**:`core/batch_download.py` 的 `_merge_overview_metadata` 把详情 Overview(优先)与 manifest metadata(补 Reading Direction 等独有字段)合成完整 `metadata` 串;同时把结构化 `overview` 字典写入 `image_record.jsonl`(供数据库消费)。
- 字段最终写入 record:`evidence`(关键词相关性,模板)、`metadata`(详情 Overview+manifest 合并)、`overview`(结构化字典)、`summary`、`collection_title`、`page_url`、`image_url`。
- 逐 item 兜底:`extract_page_to_markdown`（legacy/）从详情页 DOM 抽。
- **通用化方向**:`resolve_item_detail_overview` 是按站点可插拔的"详情元数据补充器",已由 `adapters/detail_overview.py` 通用引擎(sections/dl/table 三模式 + header_fields)驱动。IDP 与任意 `ConfigIIIFAdapter` 站点都只是 profile 配置;manifest 已含完整描述的站点(多数 IIIF 站)无需配置,零成本。

### D. 重命名逻辑（关键,常出问题）

落地即可读临时名 → 记录时改最终名,词干两处一致:
- `_titled_image_stem`(825):`序号_标题`,清洗特殊字符,截 180。
- `_final_image_filename`(842):`序号_标题_信息hash8位.ext`,hash=source_hash。
- `_rename_image_to_final_name`(857):rename + 改名后重算 sha256 校验内容未变。
- 三种 hash:`content_hash/sha256`(内容,完整性)、`source_hash`(page+image+idx,嵌文件名防串位)、`title_hash`(标题)。

### E. 存储位置逻辑

- `IMAGE_DIR`(37)默认 `image/`,`AGENT_DATA_DIR`(38)默认 `browseruse_agent_data/`,可被 env 覆盖。
- `configure_runtime_paths`(220):main 每轮指向 ImagesCache,图片与记录同目录;监工用 `BROWSER_USE_RUN_DIR` 指定。
- `_unique_path`(361)防覆盖;`.part` 临时文件原子重命名;序号 `_next_available_image_sequence`(561)避免重置/覆盖。

> 重构建议:下载方法链、信息字段、命名词干、路径解析已解耦,通用化时把 B/C 站点相关部分挪进 adapter,A/D/E 可保持站点无关。

---

## 9. 关键状态文件（每轮 ImagesCache 内）

| 文件 | 作用 |
|---|---|
| `image_record.jsonl` | 每图一条：序号/文件名/标题/URL/hash/**source_page**/overview，去重、续跑、frontier 重建依据 |
| `idp_progress.json` | next_page/next_index，续跑唯一可信源 |
| `idp_page_progress.json` | 每页统计 + frontier(可由 source_page 自愈重建) |
| `temple_photo_info.md` | 由记录重写的可读信息表 |
| `cf_storage.json` | cf_clearance 通行证 |

---

## 10. 为什么稳又快

- 批量提取/下载/去重全确定性,LLM 每页仅 1 次决策 → 慢且不稳的逐 item LLM 路径被边缘化为兜底。
- 监工多轮 + 进度文件 → 崩溃/失效自愈续跑、不重复下载。
- 方案C 自动刷新 cf_clearance → 长时间无人值守。
- 并发 4 + 可调节流 → 速度与反爬平衡。
- IIIF 通用基类 + 配置驱动 adapter → 结构相似站点可零代码/低代码接入(**通用化目标,未完成**)。

---

## 11. 通用化目标 & 当前进度

| 能力 | 状态 |
|---|---|
| IDP 单站批量 + 反CF 长稳跑 | ✅ 已跑通(MVP) |
| SiteAdapter 通用契约 / IIIF 共用基类 | ✅ 已落地 |
| ConfigIIIFAdapter(JSON 零代码接新站) | ✅ 已实现,未接线 |
| 自动 DOM profiler 生成 profile | ✅ 已实现,需人工确认 |
| 批量工具按 URL 自动选 adapter | ⏳ 待接线 |
| 多站点同时通用运行 + 站点 review 流程 | ⏳ 待完善 |

最终形态:换一个结构相似站点 → 跑 profiler 出 profile → registry 自动选 ConfigIIIFAdapter → 无需写 Python 即可批量爬。
