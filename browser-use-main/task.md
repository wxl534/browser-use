# IDP “china_buddhist” 图片下载任务

> 本文件由 `render_task.py` 从 `task_template.md` + `task_config.json` 自动生成，**请勿手改**。
> 要改搜索词 / 目标值 / 站点 / 模式 / force_generic，编辑 `task_config.json` 或用 `render_task.py` 的命令行参数，然后重新渲染。

## 任务目标

访问 **`https://idp.bl.uk/`**，搜索关键词 **`china buddhist`**，按搜索结果顺序下载 **前 n = 5000 张与“china buddhist”及其语义相关的有效图片** 到本地 `image` 目录，并提取每张图片对应的藏品信息。

本任务采用“下载即最终命名”逻辑：每成功保存一张图片后，工具会立即用该图自己的标题生成可读临时名，落地后追加其“对应信息”的 hash 作为最终文件名，并立刻写入结构化记录。`title.txt` 只作为可读导出，不再作为最终重命名依据。
本次为**批量模式（idp_batch）**：优先调用 `download_current_idp_search_page_images` 批量处理当前搜索结果页；只有批量工具无法处理某个单项时，才退回逐 item 流程（`next_search_item` + `download_image_from_url`）。

## 允许访问的网站

只允许访问以下官方域名及其页面内直接引用的官方图片资源（域名后缀白名单：["idp.bl.uk", "data.idp.bl.uk", "bl.uk"]）：

1. 目标站点 `https://idp.bl.uk/`
2. 上述白名单域名下，由站点官方直接引用的图片、IIIF、缩略图或大图资源

不要访问非官方图片搜索引擎、社交媒体或无关外站。

## 核心原则

1. 目标是 **5000 张成功保存的有效图片**，不是 5000 次尝试。
2. 搜索关键词固定为 `china buddhist`。
3. 按搜索结果顺序处理，不要随机挑选。
4. 每个详情页可保存 1 张或多张可见馆藏图片；总数达到 5000 张后停止。
5. 只保存馆藏图片，不保存 logo、按钮、Cookie 横幅、导航栏、社交分享图标、页面装饰图。
6. 每成功保存一张图片后必须立刻最终命名并记录；优先用 `download_image_from_url` 自动找图、保存、hash 去重、最终命名并记录，只有图片已通过其他方式真实落地到 `image` 目录后，才单独调用 `record_downloaded_image`。
7. `record_downloaded_image` 会计算 `content_hash` / `source_hash` / `title_hash`，把临时图片立即改为最终文件名，并自动维护 `browseruse_agent_data/image_record.jsonl`、`browseruse_agent_data/title.txt` 和 `browseruse_agent_data/temple_photo_info.md`。
8. `title.txt` 只是人工查看用的导出文件，不允许依赖它做最终重命名。
9. 不要重复处理同一详情页 URL、同一图片 URL 或同一图片内容。

## 推荐执行流程

### 阶段 1：进入站点并搜索

> 前置说明：本项目在浏览器启动前已通过取证层（Scrapling）预先注入 `cf_clearance` 通行证并对齐 User-Agent，因此通常会**直接进入站点，不会出现 Cloudflare 验证页**。如极少数情况下仍弹出验证，见下方“人机验证处理”。

1. 打开 `https://idp.bl.uk/`。
2. 如果出现 Cookie 横幅，点击同意或关闭。
3. 在网站搜索框中输入关键词 `china buddhist` 并搜索。
4. 如果搜索结果支持筛选或排序，保持默认相关性排序；如果支持每页显示数量，优先选择 50 或 100。

### 阶段 2：按顺序处理搜索结果
对搜索结果页按从上到下、从左到右的顺序处理。为了效率，必须优先使用批量工具：

```text
download_current_idp_search_page_images(
  target_count=5000,
  max_items=50,
  start_index=0,
  images_per_item=1,
  file_prefix="temple",
  title_prefix="china_buddhist",
  allowed_host_suffixes=["idp.bl.uk", "data.idp.bl.uk", "bl.uk"],
  record_filename="image_record.jsonl",
  info_filename="temple_photo_info.md"
)
```

该工具会在浏览器上下文中批量提取当前页藏品 URL、解析官方 IIIF manifest、下载主图、按 title+hash 立即最终命名并写入 `image_record.jsonl` / `title.txt`，不要再对同一页逐个结果手动点击、逐张调用 LLM 决策。

批量工具会自动维护 `browseruse_agent_data/idp_progress.json`，其中包含下一次续跑应使用的 `next_page` 和 `next_index`。断点续跑时必须优先相信这个进度文件，不要仅凭最大图片序号猜测页码。

续跑必须按 `idp_progress.json` 的页码顺序递增处理；不要自行跳到 page 999、page 5000 或其他极深页。如果某一页批量工具 0 新增、manifest 大量失败或超时，应记录该页失败并进入下一页继续批量处理，不要退化为逐个点击详情页。

如果批量工具报告当前页有个别藏品失败，再对失败项按下方“逐 item 单项流程”处理。

#### 逐 item 单项流程（批量失败项兜底；发号 + 通用下载）

0. 调用 `next_search_item()` 获取“本页下一个尚未处理的 item 的序号 + 详情页 URL”。该工具按搜索结果页 DOM 顺序（从上到下、从左到右）枚举本页 item，并交叉核对 `image_record.jsonl` 已下载记录，自动跳过已处理项，避免错位、跳过、重复循环。处理完一个 item 后，把它的详情页 URL 作为 `mark_done_url` 传回再发下一个。1. 打开 `next_search_item` 发回的那个未处理详情页。
2. 提取标题、详情页 URL、馆藏号、作者/制作者、年代、地点、分类、说明文字。
3. 确认页面与 `china buddhist` 语境相关。只要标题、说明、地点、来源等字段存在相关证据即可视为相关。
4. 优先在详情页 DOM 中查找可打开的大图、IIIF 图片、viewer 图片、download image 链接或缩略图对应的大图 URL。
5. 如果详情页包含多张相关图片，按页面图片顺序逐张保存；如果只需要单张，保存主图。
6. 调用 `download_image_from_url`（见“写入结构化记录”），保存、hash 去重、最终命名并记录。
7. 当前详情页处理完成后，把它的 URL 作为 `mark_done_url` 调 `next_search_item` 取下一个；本页取尽后进入下一页。
8. 当前页处理完后，如果总保存数量仍不足 5000，必须用 `navigate_idp_search_page(keyword="china buddhist", page=下一页, limit=50)` 跳转，不要手写搜索 URL。

## 图片保存规则

优先保存详情页或图片查看器中的最大可用图片。有效图片可能来自：

```text
官方详情页中的 img src
IIIF image URL
viewer 使用的大图 URL
download image 链接
缩略图对应的大图 URL
```

每张图片保存到 `image` 目录，文件名序号从 `temple_001` 开始递增。不能手动重置到 1；必须使用当前下一安全序号，确保不会覆盖已有图片。

保存要求：

1. 如果一页有多张相关大图，按页面图片顺序保存。
2. 优先保存原始图片字节或网站提供的大图；到达详情页或 viewer 后应直接调用 `download_image_from_url`，不要停留在图片页反复滚动。
3. `download_image_from_url` 会按学习到的顺序尝试 Python 直连、浏览器上下文 fetch、干净截图裁剪兜底；如果传入的是详情页 URL、IIIF manifest URL 或 IIIF 大图 URL，工具会优先解析真实大图 URL 并检查是否已有记录；如果工具提示“图片 URL 已有下载记录”“图片内容已有下载记录”或“image 目录中已存在相同图片内容”，视为当前图片已处理成功，直接继续下一条。
5. 如果工具返回普通失败（图片过小、像素几乎单色、非法详情页 URL 等），不要反复 scroll，重试 1 次后跳过当前图片并继续下一条。
6. 不要保存缩略图；如果只能看到缩略图，优先打开详情页大图、viewer 或下载链接。
7. 不要下载 PDF、视频、音频或网页附件。
8. 不要为了达到 n 张而保存无关图片或页面截图。

## 写入结构化记录

到达详情页、viewer 或已经拿到图片 URL 后，每张图片立即调用：

```text
download_image_from_url(
  sequence=当前下一安全序号（必须确认没有被使用，不能重置为 1）,
  file_name="temple_当前序号",
  title="china_buddhist_当前序号_藏品标题_图1",
  collection_title="页面显示的藏品标题",
  page_url="当前详情页 URL",
  image_url="从 DOM、viewer、IIIF manifest、IIIF 大图或下载链接提取到的完整 URL；如果尚未提取到可留空让工具自动找图",
  evidence="标题/说明/地点/来源中与 china buddhist 相关的证据",
  metadata="作者、时代、地点、分类、馆藏号；没有就写未显示",
  summary="图片内容和相关信息的中文简述",
  allowed_host_suffixes=["idp.bl.uk", "data.idp.bl.uk", "bl.uk"],
  record_filename="image_record.jsonl",
  info_filename="temple_photo_info.md")
```

只有图片已经用其他可靠方式保存到 `image` 目录时，才单独调用 `record_downloaded_image`；不要把它当作下载工具。

标题要求：

1. 一张图片只写一行标题，且只有图片成功保存后才写。
2. 不要提前批量写标题，不要手动写 `title.txt` 或 `END`。
3. 不要使用 `write_file` 记录标题，不要手动追加 `temple_photo_info.md`。
4. 标题应简短、稳定，避免方括号、引号、斜杠、冒号、问号、句号结尾等特殊字符。
5. 推荐格式：`china_buddhist_001_藏品标题_图1`。

## 提取并保存信息

`record_downloaded_image` 会自动根据 `image_record.jsonl` 重写 `title.txt` 和 `temple_photo_info.md`。每条记录必须包含：序号、保存文件名、重命名标题、藏品标题、藏品 URL、图片 URL、相关证据、作者/时代/地点/分类/馆藏号、简短中文说明。不要手动维护 Markdown 表格格式；只要传入字段即可。

## 失败和跳过规则

遇到以下情况必须跳过当前图片或当前详情页，不要卡住：

1. 页面没有可见馆藏图片。
2. 图片只显示为很小的缩略图，无法打开大图。
3. 图片保存失败，重试 1 次仍失败。
4. 同一详情页 URL、同一 manifest URL、同一图片 URL、同一图片内容或断点续跑上下文中列出的任一记录已经处理过。
5. 页面需要登录或付费。（遇到 Cloudflare / 人机验证页**不要直接跳过**，见下方“人机验证处理”。）
6. 页面明显与关键词 `china buddhist` 语境无关。
7. 连续出现 `browser not connected`、`No valid agent focus available`、`target may have detached` 或空白 SPA 页面时，不要继续循环恢复；调用 `done` 报告需要重启浏览器会话，并保留已下载记录。
8. 当 `download_current_idp_search_page_images` 返回的错误以 `[idp_session_corrupted]`、`[idp_extract_failed]`、`[idp_empty_page]` 或 `[idp_batch_unhandled_error]` 开头，**禁止再调用批量工具、navigate_idp_search_page 之外的任何浏览器操作来“绕过”**；必须立刻调用 `finish_download_task` 结束本次会话。
9. **禁止“手动 fallback”**：批量工具失败后，不允许通过点击 `/collection/<id>/` 详情页链接、打开 IIIF manifest 新 tab、用 `evaluate` 扫 DOM 的方式自行下载图片，以免污染浏览器上下文。

## 人机验证处理（方案C：Cloudflare 通行证已预先注入）

本项目已接入“方案C”：在浏览器启动**之前**，取证层（Scrapling）会自动通过 Cloudflare，并把 `cf_clearance` 通行证（storage_state）连同对齐的 User-Agent 一起注入浏览器。因此**正常情况下你不会看到任何人机验证页，应直接进入站点开始搜索和下载**，不要主动等待、预期或试探验证页是否出现。

只有当通行证失效（过期、出口 IP 变化或指纹被拒）时，运行中途才可能再次弹出 Cloudflare / 人机验证页。此时按以下顺序处理，**不要自己反复 `wait` 干等，也不要判定“无法绕过”而永久结束任务**：

1. 先调用**一次** `wait_for_human_verification` 做快速尝试（它会用 CDP 自动点击 Turnstile 复选框，对“单击放行”型偶尔有效）。
2. 若它返回成功（页面已恢复），继续按断点续跑上下文处理。
3. 若它返回仍未通过，**不要在本会话里反复重试点击**：直接调用 `finish_download_task` 结束本轮。外层监工（`auto_run_until_target`）检测到本轮无新增后，会**自动用 Scrapling 重新取一张新的 `cf_clearance` 通行证、带着它重启浏览器再续跑**——这才是绕过验证的可靠路径。
4. 已下载记录会完整保留，重启续跑不会丢进度，也不会重复下载。

## 明确禁止

- 默认运行模式下，旧工具（LOC / Kyohaku 等）不会注册；如需启用必须由部署者显式设置 `BROWSER_USE_ENABLE_LEGACY_TOOLS=1`。Agent 自身永远不要尝试调用这些旧工具。
- 不要在已提取到 IIIF / viewer / download image URL 后继续点击下载按钮或反复 scroll；直接调用 `download_image_from_url`。
- 不要在搜索结果页逐个点开 50 个结果；必须先调用 `download_current_idp_search_page_images` 批量处理当前页。
- 不要因为某页重复或失败就跳到 page 999 / page 5000；只能按进度文件顺序递增页码。
- 不要手写搜索分页 URL；必须使用 `navigate_idp_search_page`。
- 不要使用 `evaluate(code="自定义工具(...)")` 的写法调用工具。
- 不要使用 `write_file` 记录标题或信息表。
- 不要访问非官方图片搜索引擎、社交媒体或无关外站。
- 不要下载 PDF、视频、音频或网页附件。
- 不要重复下载同一张图。

## 完成标准

不要直接调用内置 `done`。结束任务必须调用 `finish_download_task(target_count=5000, record_filename="image_record.jsonl", title_filename="title.txt")`，它会用程序生成的确定性报告结束任务。

满足任一条件后，必须先调用 `validate_download_completion(target_count=5000, record_filename="image_record.jsonl", title_filename="title.txt")`：

1. 已成功保存 5000 张与关键词 `china buddhist` 相关的图片，并完成 `title.txt` 与 `temple_photo_info.md`。
2. 已处理完所有搜索结果页，但不足 5000 张；报告实际保存数量、跳过原因和未完成原因。
3. 网站无法访问，或人机验证在 `wait_for_human_verification` 快速尝试 + 外层监工自动刷新 `cf_clearance` 并重启多轮后仍持续无法通过。

如果 `validate_download_completion` 显示 `Final download validation: INCOMPLETE` 且 `remaining_records_needed` 大于 0，不要结束任务，继续扫描后续搜索结果并下载新图片。只有校验显示 `SUCCESS`，或确认所有结果处理完/网站不可继续访问，才调用 `finish_download_task`。

最终输出必须完全来自 `finish_download_task` 返回的确定性报告；不要自己估算或编造分页分布表，所有页号/张数必须来自真实记录。
