# IDP 图库通用图片爬虫

基于 [browser-use](https://github.com/browser-use/browser-use) 库，以 LLM 驱动的浏览器 Agent 做**确定性批量爬取**：搜索关键词 → 批量提取藏品 → 解析 IIIF manifest → 并发下载 → 去重落库，循环运行直到达成目标张数。

首个跑通站点为 [idp.bl.uk](https://idp.bl.uk/)（International Dunhuang Project）；架构面向"结构相似的 IIIF 图库站点"通用化（详见 [`WORKFLOW.md`](./WORKFLOW.md)）。

> 设计核心：**LLM 每页只决策一次「调批量工具」，其余全部确定性代码**，配合反 Cloudflare 通行证实现长时间无人值守稳定运行。

## 功能概览

- 🧭 **监工 / Worker 双层架构**：`runner.py` 多轮调度，每轮 fork 全新 `worker.py` 子进程，隔离崩溃与内存泄漏，跑到目标为止
- 📦 **整页批量下载**：`download_current_idp_search_page_images` 一次处理整页几十张图，零 LLM 逐项点击
- 🧩 **IIIF 通用适配**：`adapters/iiif.py` 解析 manifest 抽图打分，配置驱动接入新站
- 🛡️ **反 Cloudflare**：`scripts/fetch_cf_cookie.py` 取 `cf_clearance` 通行证并注入浏览器
- ♻️ **断点续跑**：基于 `image_record.jsonl` / `idp_progress.json` 自愈续跑、不重复下载
- 🔁 **单次/持续开关**：`runner.py` 的 `RUN_ONCE`（默认 0 持续跑直到达标，改 1 只跑一轮）

## 项目结构

```
browser-use-main/
├── runner.py               # 监工入口：多轮调度 + 刷新 cf_clearance（主用，含 RUN_ONCE 开关）
├── worker.py               # 单轮入口：读 task.md → 建浏览器 → 跑 Agent
├── task_config.json        # 唯一手改配置（keyword/target_count/site_url/allowed_hosts/mode）
├── task_template.md        # task.md 渲染模板
├── task.md                 # 自动生成的 Agent 指令书（勿手改）
├── resume_context_template.md  # 断点续跑上下文模板
├── core/                   # 核心：tools_registry / batch_download / concurrent_download
│                           #       idp_page_progress / task_parse / cache_layout
├── adapters/               # 站点差异层：base / iiif / idp / generic_config / registry / profile_site
├── sites/                  # 站点插件（注入下载 hint）
├── tool_actions/           # 注册给 Agent 的工具（批量下载 / 翻页 / 校验 / 过人机验证）
├── scripts/                # 辅助脚本：render_task / fetch_cf_cookie / move_images / rename_images / run_gui
├── legacy/                 # 历史遗留工具（逐 item 兜底，默认不注册）
├── webapp/                 # 爬虫 Web 控制台（backend + frontend）
├── tests/test_worker.py    # 测试脚本（不调用 LLM）
├── docs/                   # 项目文档
├── docker/                 # Docker 相关
├── browser_use/            # browser-use 库源码（本地修改版）
└── .env.example            # 环境变量模板
```

## 从零开始配置

### 前提条件

- **Python** >= 3.11（推荐 3.12+）
- **Chrome / Chromium** 浏览器（程序自动检测）
- **Git**

### 第一步：克隆并进入项目

```bash
git clone <仓库地址>
cd browser-use-main
```

### 第二步：创建并激活虚拟环境

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
```

### 第三步：安装依赖

```bash
pip install -e .
# 国内镜像加速（可选）
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 第四步：配置环境变量

```bash
cp .env.example .env
```

`.env` 中关键项（LLM 凭证，由 `worker.py` 读取）：

```bash
OPENAI_API_KEY=你的key
OPENAI_BASE_URL=https://你的兼容接口/v1   # 默认 https://openapi.seu.edu.cn/v1
```

`worker.py` 默认用 `ChatOpenAI(model='qwen3.5-397b-a17b', ...)`，可在 `worker.py` 内改模型名。任何 OpenAI 兼容接口（DeepSeek / 通义千问 / OpenAI 等）均可。

### 第五步：配置任务

只改 **`task_config.json`**，然后重新渲染生成 `task.md`：

```jsonc
{
  "keyword": "china buddhist",   // 搜索关键词
  "target_count": 5000,          // 目标图片数
  "site_url": "https://idp.bl.uk/",
  "allowed_hosts": ["idp.bl.uk", "data.idp.bl.uk", "bl.uk"],
  "mode": "idp_batch"
}
```

```bash
python scripts/render_task.py --mode idp_batch
```

> ⚠️ `task.md` 是自动生成的，**请勿手改**；改需求一律改 `task_config.json` 后重新渲染。

## 运行项目

```bash
# Windows（推荐显式指定解释器并设编码）
$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe runner.py

# macOS/Linux
python runner.py
```

**运行流程：**
1. `runner.py` 读取目标，预取/刷新 `cf_clearance` 通行证
2. 每轮 fork 一个全新 `worker.py` 子进程
3. `worker.py` 读 `task.md`、建浏览器（注入通行证）、跑 Agent
4. Agent 每页调一次批量工具，整页下载并去重落库
5. 统计新增；未达标则带新通行证继续下一轮，直到 `target_count` 达成

**单次运行**：把 `runner.py` 顶部的 `RUN_ONCE` 改为 `1`，则只跑一轮后停止（无论是否达标）。默认 `0` 持续重跑直到达标。

## 运行测试

```bash
# 不调用 LLM，验证代码功能
python -m tests.test_worker
```

当前基线：**101/101 通过**（脚本执行、路径配置、工具注册、续跑上下文、批量下载、重命名等）。

## 注册的工具

| 工具 | 用途 | 调用频率 |
|---|---|---|
| `download_current_idp_search_page_images` | **主力**：整页批量下载，零 LLM | 每页 1 次 |
| `navigate_idp_search_page` | 翻页到指定搜索页 | 按需 |
| `finish_download_task` | 收尾 | 按需 |
| `validate_download_completion` | 校验完成度 | 按需 |
| `wait_for_human_verification` | 处理人机验证 | 按需 |

> 逐个 item 兜底工具（`download_image_from_url` / `next_search_item` / `extract_page_to_markdown` 等）实测效果差、从未真正使用，已迁入 `legacy/` 且默认不注册；如需启用设环境变量 `BROWSER_USE_ENABLE_LEGACY_TOOLS=1`。

## 关键状态文件（每轮 ImagesCache 内）

| 文件 | 作用 |
|---|---|
| `image_record.jsonl` | 每图一条记录，去重与续跑的事实来源 |
| `idp_progress.json` | next_page / next_index，续跑可信源 |
| `idp_page_progress.json` | 每页统计 |
| `temple_photo_info.md` | 由记录重写的可读信息表 |
| `cf_storage.json` | Cloudflare 通行证（已被 .gitignore 忽略，勿提交） |

## 常见问题

**Q: 为什么不是 `python main.py`？**
旧版单文件入口已重构为监工/Worker 双层：日常只跑 `python runner.py`，它会自动多轮调度 `worker.py`。

**Q: 怎么改下载数量 / 搜索词 / 站点？**
改 `task_config.json` 后跑 `python scripts/render_task.py` 重新渲染，切勿直接编辑 `task.md`。

**Q: LLM 报 403 / 端点不可达？**
检查 `.env` 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`；`worker.py` 启动时会做 `preflight_llm_check` 预检端点可达性。

**Q: 一直过不了 Cloudflare？**
`cf_clearance` 绑定出口 IP，取证（`scripts/fetch_cf_cookie.py`）与主程序必须同一出口 IP；IP 信誉差时需住宅代理。

## 进一步阅读

- [`WORKFLOW.md`](./WORKFLOW.md) — 完整架构、各文件职责、通用化路线图
- [`plan.md`](./plan.md) — 清理进度与后续路线

## 许可证

本项目基于 [browser-use](https://github.com/browser-use/browser-use)（MIT License）。
