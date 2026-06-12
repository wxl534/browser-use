# Browser-Use 自动浏览器爬虫项目思路分析

可以，这个思路可行，而且比“通用浏览器 Agent”更有落地价值。它不是让 agent 随便上网，而是做一个“半结构化网站图片采集流水线”：网站都有搜索栏、图片 item、下载按钮、详情信息、数据库落库，这种任务非常适合用 `browser-use` 做导航和交互，再用确定性代码做下载、去重、解析和入库。

核心判断：不要让 LLM 负责所有步骤。LLM/Agent 适合处理“打开网站、搜索关键词、识别 item、点击详情、处理弹窗/登录/Cookie/下载按钮”这些不稳定网页交互；但下载文件、hash 去重、字段校验、数据库写入、断点续跑，应该交给普通 Python 代码确定性执行。

推荐架构：

```text
配置层
  └── site_config: 网站 URL、搜索框定位策略、item 规则、下载规则、字段规则

Agent 层 browser-use
  └── 打开网站、搜索关键词、翻页、进入 item、必要时点击下载按钮

确定性工具层 custom tools
  ├── download_image_from_url
  ├── click_download_and_capture_file
  ├── extract_item_metadata
  ├── normalize_title
  ├── compute_hash
  ├── save_image_record
  └── upsert_to_database

数据库层
  ├── sites
  ├── crawl_jobs
  ├── search_keywords
  ├── items
  ├── images
  ├── image_metadata
  └── crawl_progress
```

我建议你把系统设计成“Agent 驱动 + 工具约束 + 数据库状态机”，而不是纯 prompt 驱动。

关键原则：

1. **每个网站一个配置，不要每个网站写一套脚本**

   比如定义 `search_url`、`search_box_selector`、`result_item_selector`、`next_page_selector`、`download_button_selector`、`metadata_fields`。Agent 可以补充处理异常，但主流程应配置化。

2. **下载优先走 URL，其次才点击按钮**

   如果详情页能拿到原图 URL、IIIF manifest、JSON API、`img src`，直接用 HTTP 下载，稳定、快、便宜。只有必须触发前端下载按钮时，才让浏览器点击。

3. **数据库是唯一事实来源**

   不要靠文件名、页码、`title.txt` 判断进度。每个 item、image、source_url、content_hash 都入库。断点续跑从数据库查 `pending / done / failed / skipped`。

4. **LLM 不直接写数据库**

   LLM 可以调用 `save_image_record(...)` 工具，但工具内部必须做 Pydantic 校验、hash 去重、事务写入。不要让 Agent 自己生成 SQL。

5. **必须有去重策略**

   至少三层：

   - `item_url` 去重
   - `image_url` 去重
   - `content_hash` 去重

   否则不同页面、不同尺寸、重复馆藏很容易污染数据。

6. **爬虫合规要提前设计**

   加请求间隔、robots/条款检查、来源 URL、版权字段、license 字段、下载时间、站点名称。以后数据可用性会高很多。

数据库表可以这样设计：

| 表 | 作用 |
|---|---|
| `sites` | 目标网站配置 |
| `crawl_jobs` | 一次爬取任务，例如 site + keyword + target_count |
| `crawl_progress` | 当前页、当前 index、失败次数、续跑状态 |
| `items` | 搜索结果 item，含 item_url、title、source_site |
| `images` | 图片文件，含 image_url、local_path、content_hash、width、height |
| `metadata` | item 的作者、年代、地点、馆藏号、描述、license |
| `crawl_events` | 日志与错误，方便恢复和排查 |

未来可行性方面，我认为这个项目比单纯维护一个 browser-use fork 更值得做，因为你的价值在于：

- 多网站图片采集流程抽象
- 下载/去重/元数据/数据库的可靠流水线
- 针对文化、佛教、馆藏网站的领域规则
- 断点续跑和失败恢复

而不是重新造 browser automation 框架。

最大风险有三个：

1. **如果所有步骤都交给 Agent，会慢、贵、不稳定**

   解决：Agent 只负责网页导航和异常交互，核心数据处理工具化。

2. **不同网站结构差异很大**

   解决：做 `site adapter`，每个网站只写少量配置或插件，不要硬塞进一个超长 prompt。

3. **大规模下载涉及版权和反爬**

   解决：限速、记录来源、尊重条款、优先使用官方下载按钮/API/IIIF。

我的建议结论：可以做，而且方向是对的。但项目定位应该是：

> 一个基于 browser-use 的多站点图片馆藏采集系统，而不是一个“通用 AI 浏览器”。

这样边界清晰、工程可控、长期也有复用价值。
