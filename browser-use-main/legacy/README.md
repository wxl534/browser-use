# legacy/ — 历史脚本与旧适配器

本目录存放**当前 IDP 主线流程已不再调用**的脚本和适配器。代码仍可独立运行，但不再由
`main.py` 自动加载或注册，避免污染当前任务的工具列表与依赖图。

## 已归档脚本（无调用方）

- `apitest.py` — 早期 API 烟测脚本
- `backfill_recovered_metadata.py` — 一次性 metadata 回填
- `recover_image_records_from_files.py` — 一次性 image_record.jsonl 恢复
- `test_select_download_format.py` — LOC `select_download_format` 单元测试
- `Two_Agent_Test.py` — 双 Agent 协作实验

## 旧适配器（任务级禁用）

- `loc_tools.py`（如已迁移）— LOC (Library of Congress) 队列/下载工具集
- `kyohaku_tools.py`（如已迁移）— Kyohaku (京都国立博物馆) 抓取工具集

如果以后需要复用，可在 `main.py` 顶部 `import legacy.loc_tools` /
`import legacy.kyohaku_tools` 主动加载——只要被 import 一次，其中的
`@tools.action` 就会注册到主 `tools` 实例。
