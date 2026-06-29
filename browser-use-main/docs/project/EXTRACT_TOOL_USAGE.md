# extract_page_to_markdown 工具使用说明

## 功能概述

`extract_page_to_markdown` 是一个自定义的 browser-use 工具，用于从网页源代码中提取符合 Information.md 文件中指定的 HTML 代码块。

## 工作原理

1. **读取 Information.md 文件**：该文件包含多个 HTML 代码块，每个代码块用 ```html 标记
2. **提取首尾行**：从每个 HTML 代码块中提取第一行和最后一行作为搜索模式
3. **JavaScript 匹配**：在网页源代码中使用正则表达式查找匹配的 HTML 代码块
4. **保存结果**：将找到的匹配块保存为 Markdown、JSON 或纯文本格式

## 使用方法

### 1. 创建 Information.md 文件

在 `项目根目录\Information.md` 中定义要提取的 HTML 代码块：

```markdown
# HTML Code Blocks Information

## Block 1: 导航菜单
```html
<nav class="main-navigation">
  <ul class="nav-menu">
    <li><a href="/">首页</a></li>
```

## Block 2: 主要内容区域
```html
<main id="content" class="page-content">
  <article class="post-item">
    <h1 class="entry-title">文章标题</h1>
```
```

### 2. 在 Agent 任务中调用工具

在 task.md 或其他任务描述中，让 Agent 使用 `extract_page_to_markdown` 工具：

```
访问目标网页后，使用 extract_page_to_markdown 工具提取指定的 HTML 代码块。
```

### 3. 参数说明

工具接受以下参数（通过 ExtractPageContentParams 模型）：

- **output_filename**: 输出文件名（默认：`page_content.md`）
- **output_dir**: 输出目录（默认：`项目根目录\image`）
- **format_type**: 格式类型，可选值：
  - `markdown`（默认）：生成带格式的 Markdown 文件
  - `json`：生成结构化 JSON 文件
  - `text`：生成纯文本文件
- **information_file_path**: Information.md 文件路径（默认：`项目根目录\Information.md`）

## 输出格式

### Markdown 格式示例

```markdown
# 页面标题

**URL**: https://example.com

**找到匹配的HTML代码块数量**: 2

---

## 匹配块 1

**原始起始行**: `<nav class="main-navigation">`

**原始结束行**: `</nav>`

**提取的HTML代码**:
```html
<nav class="main-navigation">
  <ul class="nav-menu">
    <li><a href="/">首页</a></li>
  </ul>
</nav>
```

## 匹配块 2

**原始起始行**: `<main id="content" class="page-content">`

**原始结束行**: `</main>`

**提取的HTML代码**:
```html
<main id="content" class="page-content">
  <article class="post-item">
    <h1 class="entry-title">文章标题</h1>
  </article>
</main>
```
```

### JSON 格式示例

```json
{
  "page_title": "页面标题",
  "url": "https://example.com",
  "total_found_blocks": 2,
  "found_blocks": [
    {
      "original_start": "<nav class=\"main-navigation\">",
      "original_end": "</nav>",
      "content": "<nav class=\"main-navigation\">...</nav>",
      "position": 1234
    }
  ]
}
```

## 注意事项

1. **精确匹配**：HTML 代码块的首尾行必须与网页源代码中的内容完全匹配（包括空格和属性顺序）
2. **非贪婪匹配**：使用非贪婪正则表达式，确保提取最小的匹配块
3. **去重处理**：自动去除重复的匹配块
4. **错误处理**：如果未找到匹配的代码块，会返回明确的错误信息

## 与旧版本的区别

| 特性 | 旧版本 | 新版本 |
|------|--------|--------|
| 提取方式 | 基于 DOM 结构提取所有元素 | 基于 HTML 源代码匹配指定代码块 |
| 关键词过滤 | 支持关键词过滤 | 不支持（由 Information.md 控制） |
| 灵活性 | 通用提取 | 精确定位特定代码块 |
| 适用场景 | 通用网页内容提取 | 需要提取特定 HTML 结构的场景 |

## 常见问题

### Q: 为什么找不到匹配的代码块？

A: 可能的原因：
1. Information.md 中的首尾行与网页源代码不完全匹配
2. 网页是动态加载的，HTML 结构与预期不同
3. 首尾行包含了特殊字符，需要正确转义

### Q: 如何调试匹配问题？

A: 
1. 查看网页源代码（右键 -> 查看页面源代码）
2. 复制实际的 HTML 代码到 Information.md
3. 确保首尾行完全匹配（包括空格和引号）

### Q: 可以提取多少个代码块？

A: 没有数量限制，可以在 Information.md 中定义任意数量的 HTML 代码块。

## 示例任务

```python
# 在 task.md 中
"""
1. 访问 https://example.com
2. 等待页面加载完成
3. 使用 extract_page_to_markdown 工具提取指定的 HTML 代码块
4. 保存结果为 Markdown 格式，文件名为 example_blocks.md
"""
```

## 技术实现

- **JavaScript 执行**：通过 CDP (Chrome DevTools Protocol) 执行 JavaScript
- **正则表达式匹配**：使用转义后的正则表达式进行精确匹配
- **多格式输出**：支持 Markdown、JSON、Text 三种格式
- **错误处理**：完善的异常处理和错误提示
