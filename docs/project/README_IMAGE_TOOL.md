# 图片下载与重命名工具使用说明

## 功能概述

本工具实现了一个自动化的图片下载和重命名流程：

1. **提取标题**：从网页中提取图片的标题信息
2. **保存标题列表**：将所有标题保存到 `titles.txt` 文件
3. **自动下载**：通过浏览器自动下载图片到指定目录
4. **批量重命名**：运行脚本，将标题按顺序分配给下载的图片

## 快速开始

### 步骤 1：配置任务

编辑 `task.md` 或 `test.py` 中的任务描述，指定：
- 目标网站 URL
- 搜索关键词
- 需要下载的图片数量
- 图片格式要求（如 TIFF/JPEG）

### 步骤 2：运行 browser-use 提取标题并下载

```bash
python main.py
```

这个脚本会：
1. 访问目标网站
2. 提取所有图片的标题信息
3. 将标题保存到 `项目根目录\titles.txt`
4. 点击图片的下载链接，触发浏览器自动下载
5. 图片会自动保存到 `项目根目录\image` 目录

### 步骤 3：运行重命名脚本

等待 browser-use 完成下载后，执行：

```bash
python rename_images.py
```

这个脚本会：
1. 从 `titles.txt` 读取所有标题
2. 扫描 `image` 目录中的所有图片文件（按修改时间排序）
3. 将标题按顺序分配给图片
4. 重命名图片文件为对应的标题（自动清理非法字符）
5. 生成重命名记录文件 `rename_record.txt`

## 文件说明

### 主要文件

- **`test.py`**：主程序，运行 browser-use 提取标题并触发下载
- **`rename_images.py`**：重命名脚本，批量重命名已下载的图片
- **`task.md`**：任务配置文件，定义任务目标和执行步骤

### 生成的文件

- **`titles.txt`**：标题列表文件，每行一个标题
- **`image/`**：图片下载目录，包含原始下载文件和重命名后的文件
- **`rename_record.txt`**：重命名记录，包含原文件名和新文件名映射

## 配置参数

### 下载目录配置

在 `test.py` 中配置：

```python
browser = Browser(
    downloads_path=r'项目根目录\image',
    args=[
        '--download.default_directory=项目根目录\\image',
        '--download.prompt_for_download=false',
        '--disable-features=DownloadBubble,ViralVideoDownloader',
    ]
)
```

### 重命名脚本配置

在 `rename_images.py` 中配置：

```python
DOWNLOAD_DIR = r'项目根目录\image'      # 图片下载目录
TITLES_FILE = r'项目根目录\titles.txt'  # 标题文件路径
```

## 输出示例

### titles.txt 文件格式

```
Golden Buddhist Temple in Bangkok
Ancient Temple Ruins at Sunset
Meditation Hall Interior
Temple Garden with Cherry Blossoms
...
```

### 重命名后的文件

```
Golden_Buddhist_Temple_in_Bangkok.jpg
Ancient_Temple_Ruins_at_Sunset.tiff
Meditation_Hall_Interior.jpg
Temple_Garden_with_Cherry_Blossoms.jpg
...
```

### rename_record.txt 示例

```
图片重命名记录
============================================================

[1] master-afc123.jpg → Golden_Buddhist_Temple_in_Bangkok.jpg
    标题：Golden Buddhist Temple in Bangkok

[2] xyz789.tiff → Ancient_Temple_Ruins_at_Sunset.tiff
    标题：Ancient Temple Ruins at Sunset

...
```

## 错误处理

### 常见问题

1. **标题提取失败**
   - 使用 URL 中的文件名作为替代
   - 在 titles.txt 中记录为占位符

2. **下载失败**
   - 等待 30 秒后重试
   - 最多重试 2 次
   - 仍失败则跳过并继续下一个

3. **重命名失败**
   - 如果 titles.txt 不存在，脚本会提示错误并退出
   - 如果图片数量与标题数量不匹配，只处理较小数量的那一方
   - 如果文件名冲突，自动添加序号后缀

## 注意事项

1. **Windows 文件名限制**
   - 脚本会自动清理非法字符：`<>:"/\|?*`
   - 文件名长度限制为 200 字符（留 50 个给扩展名）

2. **文件排序**
   - 图片按修改时间排序（最早的在前）
   - 确保先下载的对应前面的标题

3. **备份建议**
   - 重命名前建议备份原始文件
   - 重命名记录文件可用于恢复

4. **性能优化**
   - 大批量操作时，建议分批处理
   - 避免同时下载过多文件导致超时

## 完整工作流程

```
启动 test.py
    ↓
访问目标网站
    ↓
提取图片标题 → 保存到 titles.txt
    ↓
点击下载链接 → 浏览器自动下载
    ↓
验证下载完成
    ↓
等待 user 手动执行
    ↓
运行 python rename_images.py
    ↓
读取 titles.txt
    ↓
扫描 image 目录
    ↓
按顺序重命名
    ↓
生成 rename_record.txt
    ↓
完成 ✅
```

## 故障排除

### 问题：重命名后发现文件名不对

**解决方案**：
1. 查看 `rename_record.txt` 确认映射关系
2. 检查 `titles.txt` 内容是否正确
3. 如需重新命名，先将文件恢复原始名称或使用备份

### 问题：下载的图片数量与标题数量不一致

**解决方案**：
1. 检查是否有下载失败
2. 查看 browser-use 的执行日志
3. 手动补充缺失的标题或删除多余的标题

### 问题：脚本执行时报错"文件不存在"

**解决方案**：
1. 确认 `titles.txt` 文件路径正确
2. 确认 `image` 目录存在且包含图片文件
3. 检查路径配置是否使用了正确的 Windows 路径格式

## 支持的文件格式

- JPEG: `.jpg`, `.jpeg`
- TIFF: `.tiff`, `.tif`
- PNG: `.png`
- GIF: `.gif`
- WebP: `.webp`

## 联系与支持

如有问题，请查看：
- `task.md` - 详细的任务配置说明
- `rename_images.py` - 脚本源码和注释
- `test.py` - 主程序逻辑
