# 通用最强下载工具改造（download_image_from_url）

目标：把批量工具的效率本钱搬进逐项通用工具 `download_image_from_url`，并把 IDP 专用 hint 解耦，使其成为唯一的通用下载工具。批量工具暂不删，验证通用流程跑通后再决定。

## Part A — 效率：缓存索引去重 + 快速落库
现状：download_image_from_url 每次调用把 image_record.jsonl 重读 ~3 遍（image_url / file_hash / record 落库各扫一次）+ 重扫 IMAGE_DIR，逐项 N 张图是 O(N²)。

改造：
- [ ] tools_registry.py 新增 module 级缓存 `_GENERIC_DOWNLOAD_INDEX_CACHE`
  - `_get_cached_download_index(record_filename)` -> (index, existing_image_hashes, cache_entry)
  - 失效条件：record_file mtime 变化 或 IMAGE_DIR 变化（configure_runtime_paths）
  - `_refresh_generic_index_mtime(cache_entry)`：本工具自己 append 写后刷新缓存 mtime，避免下次误判失效重建
  - 复用现成 `_build_download_record_index` / `_build_existing_image_hash_index`
- [ ] 新增 `_safe_requested_image_sequence_from_index(requested, index, file_prefix)`：用 index.max_sequence 取下一安全序号，免重读 JSONL
- [ ] download_image_from_url：
  - 去重 A（下载前 image_url）→ `index.records_by_image_url.get(image_url)`
  - 去重 B（下载后 content sha256）→ `index.records_by_file_hash.get(file_hash)`
  - 去重 C（下载后磁盘 sha256）→ `existing_image_hashes.get(file_hash)`
  - 落库 → `_record_saved_image_fast(record_index=index, existing_image_hashes=...)` 取代 `_record_saved_kyohaku_image`
  - 落库成功后刷新缓存 mtime
  - 保留所有现有返回信息/语义

## Part B — 解耦 IDP hint，使下载工具纯通用
- [ ] tools_registry.py 新增 host-keyed 注册表 `_SITE_DOWNLOAD_HINTS` + `register_download_site_hint(...)`
  - `_site_manifest_url_from_page_url(url)` / `_site_invalid_collection_url(url)` 通用分发
  - 注册 IDP 实现（复用现有 `_idp_manifest_url_from_page_url` / `_is_invalid_idp_collection_url`），保证 IDP 行为不变
- [ ] download_image_from_url 改用通用分发函数，去掉文件内 IDP 命名；manifest_note 文案改通用
- [ ] tools_registry 现有 IDP helper 保留（navigate / _choose_reliable_page_url 仍用），不动其它调用方

## 验证
- [ ] python -c 导入 tool_actions.download_image_from_url / tools_registry OK
- [ ] 比对 tools 注册 action 列表与基线一致
- [ ] py_compile 全量
- [ ] 跑 test_main.py（基线 101/102，保持不退化）
- [ ] 默认不 push（除非用户要求）
