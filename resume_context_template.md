## 断点续跑上下文

用户选择了接着上一次断点运行.你必须把下面的记录视为已经成功下载,不能重新下载或重新记录.

- 记录文件：`{record_file}`
- 已成功记录图片数：{record_count}
- 本地 image 目录图片数：{image_count}
- IDP 进度文件：`{idp_progress_path}`
- 建议续跑搜索页：page={suggested_page}, start_index={suggested_start_index}
- 已使用最大序号：{max_sequence}
- 下一张新图片必须从序号：{next_sequence} 开始，例如 `temple_{next_sequence:03d}` / `{title_prefix}_{next_sequence:03d}_...`
- 程序已在 Agent 启动前按页级进度预执行第一批动作；Agent 接手后必须从最新 `idp_progress.json` 继续。
- 恢复后不要从搜索结果第 1 页重新扫描；如需继续批量处理，使用 `navigate_idp_search_page(keyword="{search_keyword}", page={suggested_page}, limit=50)` 和 `download_current_idp_search_page_images(..., start_index={suggested_start_index}, ...)`。
- 必须按 `idp_page_progress.json` 同步导出的 page={suggested_page} 顺序递增；不要自行跳到 page 999、page 5000 或其他极深页。
- 如果当前页批量工具 0 新增或失败,改为下一页继续批量处理;不要退化为逐个点击详情页.
- 当 `download_current_idp_search_page_images` 返回的错误以 `[idp_session_corrupted]`,`[idp_extract_failed]`,`[idp_empty_page]` 或 `[idp_batch_unhandled_error]` 开头时,立刻调用 `finish_download_task`;不要尝试手动点击 collection 详情页,IIIF manifest tab 或 evaluate 扫 DOM.
- 最终报告里的图片数和页分布只能来自 `image_record.jsonl` / `idp_progress.json` / `idp_page_progress.json` 真实记录,禁止编造.
- 按有效记录数距离目标 {target_image_count} 还需要继续处理：{remaining_by_count} 张；不要因为最大序号达到目标就提前结束，也不要回头补齐旧序号空洞
- 上一次最后记录：#{last_sequence:03d}，文件 `{last_file_name}`，标题 `{last_title}`，页面 `{last_page_url}`

续跑规则:
1. 不要清空 image 目录,image_record.jsonl 或 temple_photo_info.md.
2. 重复判断由下载工具基于 image_record.jsonl 内存索引执行;不要要求自己记忆历史 URL,也不要回头重扫旧页.
3. 如果误点到已处理详情页,调用工具后返回“图片 URL 已有下载记录”或“详情页已处理”时,视为成功跳过并继续下一条.
4. 第一次保存新图片时传入 sequence={next_sequence}，之后按安全序号递增；如果工具自动修正序号，以工具返回为准；目标以有效记录数量为准，不以文件夹文件数或最大序号为准。
