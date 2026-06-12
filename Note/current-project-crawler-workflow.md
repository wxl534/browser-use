# 当前项目爬虫具体工作流程分析

目前这个项目的爬虫流程是：`main.py` 读 `task.md`，启动 browser-use Agent；Agent 按提示操作 IDP 搜索页；真正的批量下载、去重、记录、落库主要由 `tools_registry.py` 里的确定性工具完成。

具体链路如下：

```text
task.md
  ↓
main.py 读取任务、目标数量 n、关键词 ssu
  ↓
选择 Images\ImagesCache 作为本次运行目录
  ↓
创建 Browser + ChatOpenAI + Agent + 自定义 tools
  ↓
Agent 打开/跳转 IDP 搜索页
  ↓
优先调用 download_current_idp_search_page_images
  ↓
工具批量提取搜索结果 item
  ↓
解析每个 item 的 IIIF manifest
  ↓
下载图片
  ↓
hash 去重 + 最终命名 + 写 image_record.jsonl/title.txt/temple_photo_info.md
  ↓
更新 idp_progress.json
  ↓
任务结束后校验结果并导入 SQLite
```

核心流程分 6 层：

## 1. 任务读取层

`main.py` 从 `task.md` 读取任务。当前任务是访问 `https://idp.bl.uk/`，搜索关键词 `ssu`，目标下载 `n = 5000` 张相关图片。程序会自动解析目标数量和搜索关键词。

## 2. 运行状态层

当前项目已经不是简单写到 `image/`，而是把本次运行集中到：

```text
Images\ImagesCache\
```

这里同时作为图片目录和数据目录。里面会放：

- 图片文件
- `image_record.jsonl`
- `title.txt`
- `temple_photo_info.md`
- `idp_progress.json`
- `final_download_report.md`
- `image_catalog.sqlite3`

如果不是续跑，旧的 `ImagesCache` 会按关键词归档；如果选择续跑，会保留现有记录并继续。

## 3. Agent 控制层

`main.py` 创建 `Browser` 和 `Agent`。当前配置是：

- 本地浏览器，`headless=False`
- 禁用 vision：`use_vision=False`
- 下载路径指向 `Images\ImagesCache`
- LLM 使用 `ChatOpenAI`
- 模型是 `qwen3.5-397b-a17b`
- base_url 默认是 `https://openapi.seu.edu.cn/v1`

所以 Agent 主要靠 DOM/工具返回结果决策，而不是看截图。

## 4. IDP 批量下载层

最重要的工具是：

```text
download_current_idp_search_page_images
```

它不是让 Agent 一个个点击详情页，而是批量做这些事：

- 从当前 IDP 搜索结果页提取 `/collection/` 藏品 item
- 找到每个 item 的 IIIF manifest URL
- 在浏览器上下文里 fetch manifest
- 从 manifest 解析真实图片 URL
- 下载图片
- 每个 item 默认保存 1 张图
- 写入记录
- 更新进度

这是目前项目里最接近“真正爬虫核心”的部分。

## 5. 单图兜底层

如果批量工具处理不了某个 item，Agent 可以调用：

```text
download_image_from_url
```

这个工具更通用，支持：

- 直接图片 URL
- IIIF manifest URL
- IDP 详情页 URL
- 当前页面自动找图
- Python 直连下载
- 浏览器上下文 fetch
- 干净截图裁剪兜底

它适合处理异常页面、非 IDP 网站，或者必须从页面 DOM 里临时找图片的情况。

## 6. 记录、校验、落库层

每张图片保存成功后会进入记录流程：

- 校验图片文件是否有效
- 计算 SHA256 / content_hash
- 检查 `image_url` 是否重复
- 检查图片内容 hash 是否重复
- 用 `title + short_hash` 做最终文件名
- 写入 `image_record.jsonl`
- 同步生成 `title.txt`
- 同步生成 `temple_photo_info.md`

Agent 结束后，`main.py` 会调用最终校验，并运行：

```text
import_records_to_sqlite.py
```

把 `image_record.jsonl` 和本地图片导入：

```text
Images\ImagesCache\image_catalog.sqlite3
```

目前这个项目的实际模式可以概括为：

> Agent 负责进入网站、搜索、翻页、决定下一步；确定性工具负责批量提取、下载、去重、记录、校验、落库。

这是正确方向。问题是现在它仍然强绑定 IDP：`task.md`、`download_current_idp_search_page_images`、`navigate_idp_search_page`、IIIF manifest 解析、进度文件字段都围绕 IDP 写死。下一步如果要做成多网站项目，应该把 IDP 这套逻辑抽成一个 `SiteAdapter`，再做通用的下载/去重/数据库层。
