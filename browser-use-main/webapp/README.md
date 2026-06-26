# IDP 爬虫 Web 控制台

为图片爬虫提供的可视化控制台：仪表盘统计、图片画廊、元数据表、运行历史，以及从浏览器启动/停止任务并实时查看日志。

- **后端**：FastAPI（`webapp/backend/`），只读访问 `Images/ImagesCache/image_catalog.sqlite3` + `image_record.jsonl`，并托管 supervisor（`auto_run_until_target.py`）进程。
- **前端**：React + Vite + TypeScript（`webapp/frontend/`），使用 react-router、@tanstack/react-query、recharts。

所有路径都相对项目根（`webapp/backend/paths.py` 推导），无硬编码绝对路径。

## 目录

```
webapp/
  backend/
    app.py            FastAPI 路由（数据 API / 控制 API / SSE 日志 / 静态托管）
    catalog.py        只读 SQLite 查询层
    run_manager.py    supervisor 进程托管 + 日志环形缓冲 + SSE 广播
    paths.py          集中路径解析
    make_sample_data.py  开发用样例数据生成器
    requirements.txt
  frontend/
    src/
      api.ts          typed fetch 客户端
      types.ts        与后端返回结构对齐的 TS 类型
      App.tsx         侧栏导航 + 路由
      views/          Dashboard / Gallery / Records / Runs / Control
      index.css       全局样式（深色主题）
```

## 开发模式（前后端分离，热重载）

需要两个终端。统一在 `browser-use-main` 目录下，并设 `PYTHONIOENCODING=utf-8`。

终端 1 — 后端（:8000）：

```powershell
cd browser-use-main
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m uvicorn webapp.backend.app:app --host 127.0.0.1 --port 8000 --reload
```

终端 2 — 前端 dev server（:5173，已配 `/api` 反向代理到 :8000）：

```powershell
cd browser-use-main\webapp\frontend
npm install      # 首次
npm run dev
```

浏览器打开 http://127.0.0.1:5173 。

## 生产模式（单进程托管）

构建前端后由 FastAPI 直接托管 `dist/`（含 SPA 深链回退）：

```powershell
cd browser-use-main\webapp\frontend
npm run build
cd ..\..
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m uvicorn webapp.backend.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000 。

## 样例数据（无真实抓取时预览）

```powershell
cd browser-use-main
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe webapp\backend\make_sample_data.py
```

会在真实 `ImagesCache` 目录生成 6 张 PNG + jsonl，并调用导入脚本建 SQLite 库。一次真实 `--new-run` 会归档清除这些样例。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stats` | 汇总统计（已下载/目标/按状态/按站点/文件大小/孤儿/断点进度） |
| GET | `/api/images` | 图片分页（`page` `page_size` `q` `status` `sort` `order`） |
| GET | `/api/images/{id}` | 单条元数据 |
| GET | `/api/images/{id}/file` | 图片文件（带路径穿越校验） |
| GET | `/api/runs` | 导入批次历史 |
| GET | `/api/progress` | 断点进度 + 最终报告 |
| GET | `/api/run/status` | supervisor 运行状态 |
| POST | `/api/run/start` | 启动 supervisor（body 为运行参数 JSON） |
| POST | `/api/run/stop` | 停止 supervisor |
| GET | `/api/logs/recent` | 最近日志快照 |
| GET | `/api/logs/stream` | SSE 实时日志流 |
| GET | `/api/health` | 健康检查 |

## 端口约定

- 后端 uvicorn：`127.0.0.1:8000`
- 前端 vite dev：`5173`（代理 `/api` → `8000`）
- 生产：`npm run build` → `dist/`，FastAPI 在 `dist` 存在时托管 `/`

## 控制台运行参数

控制台表单复刻 `run_gui.py` 的字段：关键词、目标数量、新任务/断点续传、爬取模式（`generic_per_item` / `idp_batch`）、最大轮数、超时、无进展上限、间隔、翻页延迟、冷却、下载并发；以及可选环境变量（`OPENAI_API_KEY`、`OPENAI_BASE_URL`、Chrome 路径/Profile、代理、Scrapling、Cloudflare 取证 URL、Storage State）。
