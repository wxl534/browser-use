# 项目清理与路线图 plan.md

> 通用图库爬虫（IDP 为已跑通 MVP，目标推广到结构相似 IIIF 站点）。
> 详细架构见 WORKFLOW.md。本文件只跟踪「清理进度 + 后续路线」。

## 1. 已完成（代码结构清理）
- [x] main.py → worker.py（单轮入口）、auto_run_until_target.py → runner.py（监工）
- [x] runner.py 加 RUN_ONCE 开关（默认 0 持续跑，改 1 单轮停）
- [x] 逐 item 兜底工具迁入 legacy/ 且默认不注册（download_image_from_url / next_search_item / extract_page_to_markdown），record_downloaded_image 受 BROWSER_USE_ENABLE_LEGACY_TOOLS 控制
- [x] 公共逻辑下沉 core/（task_parse.py、cache_layout.py），worker/runner 去重
- [x] 删死代码：worker 的 rotate_large_logs / 输入监听线程 / 重复 count；runner 6 个与 core 重复的函数
- [x] 注释 + 字符串全角中文标点 → 英文标点
- [x] info.md 删除；Information.md 移入 legacy/ 并清掉活跃引用
- [x] 基线测试 101/101 通过（python -m tests.test_worker）

## 2. 待做（继续减负，低风险）
- [ ] runner.read_downloaded_count 与 core/cache_layout.count_downloaded_records 合并
- [ ] archive_cache / archive_cache_for_keyword_change 下沉公共模块
- [ ] 复核 legacy/ 是否还有可彻底删除的死工具

## 3. 路线图（通用化，主线）
- [ ] 批量工具改用 registry.resolve_adapter 自动选 adapter（现硬编码 IDPAdapter）
- [ ] profile_site.py 跑通「DOM → profile 草稿 → 人工确认」闭环
- [ ] ConfigIIIFAdapter 接第二个 IIIF 站点验证零代码接入
- [ ] 多站点同时运行 + 站点 review 流程

## 4. 维护须知（勿删）
- pyproject.toml：browser_use v0.11.9 包定义，.venv 由此 editable 安装，删则环境废
- task_config.json：唯一手改配置（keyword/target/site），render_task/runner/webapp 调用
- 运行：.\.venv\Scripts\python.exe，设 PYTHONIOENCODING=utf-8；改配置后 python scripts/render_task.py 重渲染 task.md，勿手改 task.md
- 默认不 push GitHub
