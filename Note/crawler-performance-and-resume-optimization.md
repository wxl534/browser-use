# 爬虫性能与断点续跑优化分析

## 核心结论

当前项目“下载的图片越多就越慢”的核心原因，主要不是网络下载本身，而是每下载一张新图时，程序都会在越来越大的历史记录和图片目录上做重复全量检查。

当前主要瓶颈包括：

1. 反复读取并解析 `image_record.jsonl`
2. 反复扫描 `Images\ImagesCache` 图片目录
3. 反复对历史图片重新计算 SHA256
4. 断点恢复仍部分依赖 Agent 按 prompt 执行，而不是完全由确定性代码调度

这些问题会让流程从理想的近似线性复杂度，退化成越来越接近 `O(n²)` 的行为。目标是 5000 张图片时，后半程会明显变慢。

---

## 1. 为什么反复读 `image_record.jsonl` 会越来越慢

当前批量工具 `download_current_idp_search_page_images` 内部会多次调用类似函数：

```python
_read_downloaded_records(...)
_find_downloaded_record_by_image_url(...)
_find_downloaded_record_by_file_hash(...)
_next_available_image_sequence(...)
```

这些函数大多不是查内存，而是每次重新读取整个 `image_record.jsonl`。

例如：

```python
def _read_downloaded_records(record_file):
    for line in record_file.read_text(encoding='utf-8').splitlines():
        ...
```

这意味着假设已经下载 4000 张图，处理一张新图时可能会发生：

- 检查目标数量：读 4000 行
- 检查 `image_url` 是否重复：读 4000 行
- 计算下一个 sequence：读 4000 行
- 检查 content hash 是否重复：读 4000 行
- 记录阶段又可能再读 4000 行

处理越往后，每张图的历史检查成本越高。

---

## 2. 更重的瓶颈：反复扫描目录并重算历史图片 hash

当前逻辑中还有类似函数：

```python
def _find_existing_image_file_by_hash(file_hash, exclude_path=None):
    for image_path in IMAGE_DIR.iterdir():
        if _sha256_file(image_path) == file_hash:
            return image_path
```

这个函数会扫描整个图片目录，并对历史图片重新计算 SHA256。

如果目录里已有 4000 张图片，每次新下载一张都重新扫描这些文件，就会导致大量无意义磁盘读取。图片越多，单张处理越慢。

实际上，`image_record.jsonl` 里已经保存了：

```json
{
  "sha256": "...",
  "content_hash": "..."
}
```

所以常规重复判断应该优先使用记录中的 hash，而不是每次重新读所有图片文件。

---

## 3. 优化一：批量工具启动时构建内存索引

当前模式：

```text
每次判断重复
  ↓
重新读取 image_record.jsonl
  ↓
遍历全部历史记录
```

优化后：

```text
批量工具开始
  ↓
读取 image_record.jsonl 一次
  ↓
构建内存索引
  ↓
处理当前页所有图片
  ↓
每成功一张，更新内存索引 + 追加写 JSONL
```

建议构建的索引：

```python
@dataclass
class DownloadRecordIndex:
    downloaded_count: int
    max_sequence: int
    image_urls: set[str]
    file_hashes: set[str]
    source_hashes: set[str]
    used_sequences: set[int]
    records_by_image_url: dict[str, dict]
    records_by_hash: dict[str, dict]
```

使用方式：

```python
record_index = _build_download_record_index(params.record_filename)

if record_index.downloaded_count >= params.target_count:
    break

if image_url in record_index.image_urls:
    skipped.append(...)
    continue

sequence = record_index.max_sequence + 1
```

成功记录后同步更新：

```python
record_index.downloaded_count += 1
record_index.max_sequence = max(record_index.max_sequence, sequence)
record_index.image_urls.add(image_url)
record_index.file_hashes.add(file_hash)
record_index.used_sequences.add(sequence)
```

这样重复判断可以从：

```text
每张图 O(历史记录数)
```

变成：

```text
每张图 O(1)
```

---

## 4. 优化二：图片目录 hash 只扫描一次

当前 `_find_existing_image_file_by_hash(...)` 每次都会扫描整个图片目录。

建议改成：

```python
def _build_existing_image_hash_index() -> dict[str, Path]:
    result = {}
    for image_path in IMAGE_DIR.iterdir():
        if image_path is valid image:
            result[_sha256_file(image_path)] = image_path
    return result
```

批量工具开始时：

```python
existing_file_hashes = _build_existing_image_hash_index()
```

每次新下载后：

```python
if file_hash in existing_file_hashes:
    duplicate_file = existing_file_hashes[file_hash]
    image_path.unlink()
    skip
else:
    existing_file_hashes[file_hash] = image_path
```

这样最多只在批量工具开始时扫描一次目录，不会每张图都扫描 5000 个历史文件。

进一步优化：如果已经确认 `image_record.jsonl` 是唯一事实来源，可以只在启动时做一次“记录与文件一致性校验”，后续主要依赖记录里的 hash。

---

## 5. 优化三：JSONL 追加写，Markdown 最后生成

当前部分记录逻辑会重写记录文件、重写 `title.txt` 和 `temple_photo_info.md`。对小规模任务没问题，但对 5000 张图片会越来越慢。

推荐策略：

```text
image_record.jsonl：成功一张 append 一行
title.txt：可以 append，或每 100 张重建一次
temple_photo_info.md：不要每张都完整重写，最后统一生成
SQLite：成功一张立即 upsert
```

`temple_photo_info.md` 是展示型文件，不应该成为主流程性能瓶颈。主流程应优先保证结构化记录和数据库正确。

---

## 6. 优化四：小并发处理 manifest 和图片下载

当前 IDP 批量工具基本是顺序处理：

```text
item1 manifest → image download → record
item2 manifest → image download → record
item3 manifest → image download → record
```

可以使用小并发：

```python
semaphore = asyncio.Semaphore(3)
```

示意：

```python
async def process_item(item):
    async with semaphore:
        fetch manifest
        resolve image_url
        download image
        compute hash
        record
```

注意不要无限并发。IDP / British Library 这类机构站点建议并发数为 `3` 到 `5`，避免触发限流或导致下载失败。

如果需要严格保持文件序号按搜索结果顺序，可以采用：

```text
并发 fetch manifest / image_url
顺序分配 sequence 和记录
```

---

## 7. 当前断点续跑逻辑的问题

当前项目已有较好的断点基础：

- `Images\ImagesCache` 保存当前运行
- `image_record.jsonl` 记录已下载图片
- `idp_progress.json` 记录 `next_page` / `next_index`
- `idp_page_progress.json` 记录页级状态
- resume 时把上下文追加到 task

但仍有几个可优化点。

### 7.1 断点恢复仍依赖 Agent 听 prompt

当前 `build_resume_task_context(...)` 会生成类似提示：

```text
先调用 navigate_idp_search_page(...)
然后立即调用 download_current_idp_search_page_images(...)
不要从 page=1 重新处理
```

这比没有断点提示好很多，但本质上仍依赖 LLM 执行。LLM 可能：

- 忘记调用批量工具
- 先访问首页
- 把 page 写错
- 忘记传 `start_index`
- 退化为逐个点击详情页

更稳定的方式是：断点恢复第一步由 Python 代码直接执行，而不是写在 prompt 里让 Agent 执行。

理想流程：

```text
main.py 读取 idp_progress.json / idp_page_progress.json
  ↓
程序确定 page/start_index
  ↓
程序先执行 navigate_idp_search_page
  ↓
程序先执行 download_current_idp_search_page_images
  ↓
如果异常，再让 Agent 介入
```

---

### 7.2 页级进度状态机没有被主流程充分使用

项目里已有 `idp_page_progress.py`，其中有：

```python
select_next_page(...)
mark_page_batch_result(...)
```

这个页级状态机可以避免 LLM 跳到极深页，也能标记：

- `pending`
- `in_progress`
- `done`
- `blocked`
- `failed`

但目前主 `main.py` 的 resume 主要读 `idp_progress.json`，而 `select_next_page(...)` 主要在 `auto_run_until_target.py` 中使用。

这导致有两套进度来源：

```text
idp_progress.json
idp_page_progress.json
```

风险：

- 两者不一致时不知道信谁
- 手动运行和自动运行的恢复路径不完全一致
- Agent 看到的是一个进度，自动调度器可能使用另一个进度

建议统一为：

```text
idp_page_progress.json 是页级真相
image_record.jsonl 是图片级真相
idp_progress.json 只是兼容导出
```

启动时统一调用：

```python
active = select_next_page(...)
```

然后把 active 同步到 `idp_progress.json`。

---

### 7.3 resume 目录选择可以更精确

当前 `select_active_cache_dir(...)` 在 resume 时会找第一个存在且未锁的候选目录：

```text
ImagesCache
ImagesCache_01
ImagesCache_02
...
```

如果存在多个缓存目录，它可能选到第一个存在的，不一定是最近一次或记录最多的一次。

建议 resume 时扫描所有候选目录，并按优先级选择：

```text
1. 有 run.lock 且进程还活着：跳过
2. 有 image_record.jsonl
3. downloaded 记录最多
4. updated_at 最新
5. 有 idp_progress.json / idp_page_progress.json
```

如果发现多个可续跑目录，应打印选择依据：

```text
发现多个可续跑目录：
1. ImagesCache downloaded=3200 updated=...
2. ImagesCache_01 downloaded=800 updated=...
默认选择 ImagesCache
```

---

### 7.4 断点上下文不应列出太多历史记录

当前 resume prompt 会列出最近 120 条记录：

```python
listed_records = records[-120:]
```

这会增加上下文长度，也可能干扰 Agent。

如果工具层已经有内存索引，Agent 不需要记住这些历史 URL。

更好的 resume prompt 只保留：

```text
- downloaded_count
- next_page
- next_index
- next_sequence
- 不要回头
```

重复判断应该交给工具，不交给 Agent 记忆。

---

## 8. 推荐的整体架构升级

当前流程：

```text
Agent 读 task
  ↓
Agent 决定调用 navigate
  ↓
Agent 决定调用批量下载
  ↓
工具执行
  ↓
Agent 再决定下一页
```

推荐升级为：

```text
Python 调度器读取 JSONL / SQLite / 页级进度
  ↓
Python 决定 page/start_index
  ↓
Python 调用批量下载工具
  ↓
成功：继续下一页
  ↓
失败：才把当前页面交给 Agent 处理异常
```

也就是：

> 确定性代码负责主循环，Agent 负责网页异常。

这样可以减少：

- LLM step 数
- prompt 偏移
- 错误跳页
- 重复扫描
- 无意义等待
- 上下文污染

---

## 9. 建议改造优先级

### 第一优先级：解决 O(n²) 性能问题

给 `download_current_idp_search_page_images` 加内存索引：

- 一次读取 `image_record.jsonl`
- 构建 `image_url/hash/sequence` set
- 循环内不再重复读记录

### 第二优先级：图片目录 hash 一次性索引

- 批量工具开始时扫一次图片目录
- 后续查 dict
- 不再每张图扫描整个目录

### 第三优先级：减少完整校验频率

- `validate_download_artifacts(...)` 每页一次即可
- 重复图片 hash group 检查放到最终阶段

### 第四优先级：统一断点进度来源

- `idp_page_progress.json` 作为页级真相
- `image_record.jsonl` 作为图片级真相
- `idp_progress.json` 仅作为兼容导出

### 第五优先级：把 resume 首次动作从 prompt 移到代码

- 不再要求 Agent 自己先 navigate
- 代码直接决定并执行 page/start_index
- Agent 只处理异常情况

### 第六优先级：小并发下载

- 先解决重复扫描问题
- 再做 `asyncio.Semaphore(3)` 或 `Semaphore(5)`

---

## 最终建议

短期最值得做的是：

```text
内存索引 + 图片目录 hash 一次性索引 + 断点进度统一
```

这三项能直接解决“越下载越慢”和“断点重跑不够确定”的问题。

长期应该把项目改成：

```text
确定性调度器 + SiteAdapter + Agent 兜底 + SQLite 状态机
```

这样它才会从“能跑的 Agent 爬虫脚本”升级为“稳定、可恢复、可扩展的多站点图片采集系统”。
