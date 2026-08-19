任务：根据图片中的编号范围，从页面 https://news-sv.aij.or.jp/da2/yachou/gallery_3_chuta2.htm 下载所列条目的图片与文本（23-28 为纯文字）。

目标编号：2-5, 14-22, 23-28 (纯文字), 29, 67-69, 75

总体策略（根据你测试：图片 URL 隐藏，需在条目页面通过交互或脚本揭露并下载）：

1) 读取主目录页面（Shift_JIS 编码），解析表格中每行编号与对应的相对链接（Gallery_3_chuta2-<N>k.htm）。只处理目标编号列表中的条目。
2) 对每个条目（按目标编号顺序）：
   a) 打开条目页面（HTTP GET）；解析并记录页面文字信息（title、说明、caption 等），保存为 UTF-8 文本/JSON。
   b) 在条目页面查找图片显示区域：若 HTML 中能直接找到大图 URL（<img src> 或直接链接），记录并下载；否则用浏览器自动化打开条目页面并模拟必要的点击/交互以触发或揭露图片 URL，然后记录并下载该 URL（图片必须通过 URL 下载）。
   c) 将该条目的图片保存到以该条目标题命名的子文件夹（非法文件名字符做安全化），所有子文件夹置于一个统一的根目录（例如: YDZT_downloads/）。
3) 对于 23-28（纯文字）：抓取页面文字并保存为 UTF-8 文本文件，仍放在对应标题文件夹下。
4) 文件命名与元数据：
   - 目录：YDZT_downloads/{safe_title}/
   - 图像文件名：{safe_title}_p{item}_{index}.{ext}
   - 同目录生成 metadata_{item}.json，包含 item_number, title, caption, source_page, image_url(s), local_paths, fetched_at。
5) 重试与错误处理：请求超时 30s，遇网络错误或 5xx 重试最多 3 次。若服务器返回人机验证/JS challenge，切换到浏览器自动化并（如可用）注入 IDP_STORAGE_STATE/已保存 storage_state 以复用通行证。
6) 并发与速率：默认并发 3 个条目并行下载（可配置），总速率限制 2 req/s。浏览器自动化路径保持单线程以避免并发 CDP 限制。
7) 完成报告：生成 CSV（item, title, images_downloaded, text_saved, errors）并写入 root 目录。

附注：按你测试的结论，本任务需要在多数条目上用浏览器交互来揭露图片 URL，脚本应优先尝试静态解析再回退到自动化。注意一定要确定url地址正确，并且确定不漏记录图片，比如一个项目里有10张图片，就一定要记录10个对应url。