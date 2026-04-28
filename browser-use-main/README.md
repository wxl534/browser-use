# Browser-Use 图片自动下载工具

基于 [browser-use](https://github.com/browser-use/browser-use) 库，通过 LLM 驱动浏览器 Agent 自动完成：网页图片搜索、下载、信息提取、文件重命名的全流程自动化。

## 功能概览

- 🤖 **LLM 驱动**：Agent 自动理解任务、操作浏览器、执行下载
- 📥 **自动下载**：识别并点击下载按钮，图片保存到 `image/` 目录
- 📄 **信息提取**：自定义工具 `extract_page_to_markdown`，基于 `Information.md` 中的模式从网页源码提取内容
- 🏷️ **自动重命名**：Agent 完成后自动调用脚本，根据 `title.txt` 将图片重命名为有意义的标题
- 🛑 **优雅退出**：运行过程中输入 `quit` 即可安全停止

## 项目结构

```
browser-use-main/
├── main.py                # 主程序入口
├── tools_registry.py      # 自定义工具注册（extract_page_to_markdown）
├── move_images.py         # Agent 运行前清理 image/ 目录
├── rename_images.py       # Agent 运行后根据 title.txt 重命名图片
├── task.md                # 任务描述文件（Agent 读取此文件作为指令）
├── task1.md               # 备选任务模板
├── Information.md         # HTML 提取模式配置（自定义工具使用）
├── test_main.py           # 测试脚本（不调用 LLM）
├── .env.example           # 环境变量模板
├── pyproject.toml         # 项目依赖配置
├── browser_use/           # browser-use 库源码（本地修改版）
├── image/                 # 下载的图片（自动创建）
├── history/               # 历史图片存档（自动创建）
├── browseruse_agent_data/ # Agent 运行数据（title.txt 等）
└── browser_profile/       # 浏览器用户数据（自动创建）
```

## 从零开始配置

### 前提条件

- **Python** >= 3.11（推荐 3.12+）
- **Chrome / Chromium** 浏览器（已安装即可，程序自动检测）
- **Git**（用于克隆项目）

### 第一步：克隆项目

```bash
git clone https://github.com/你的用户名/browser-use.git
cd browser-use/browser-use-main
```

### 第二步：创建虚拟环境

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
```

### 第三步：安装依赖

```bash
# 以开发模式安装（包含所有依赖）
pip install -e .

# 如果安装较慢，可使用国内镜像
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 第四步：配置环境变量

```bash
# 复制模板
cp .env.example .env
```

编辑 `.env` 文件，配置日志级别等选项（默认配置即可运行）。

### 第五步：配置 LLM(无需修改)

在 `main.py` 中配置你的大模型。默认使用 OpenAI 兼容接口：

```python
# main.py 中修改以下内容
api_key = '你的API密钥'
base_url = 'https://你的API地址/v1'

llm = ChatOpenAI(
    model='模型名称',
    api_key=api_key,
    base_url=base_url,
    temperature=0.0
)
```

**支持的 LLM 配置方式：**

| 方式 | 说明 |
|------|------|
| `ChatOpenAI(api_key=..., base_url=...)` | 任何 OpenAI 兼容 API（推荐） |
| `ChatBrowserUse()` | browser-use 官方 LLM（需付费订阅） |
| 环境变量 `OPENAI_API_KEY` | 直接使用 OpenAI 官方 |

**常见 LLM 服务示例：**

```python
# DeepSeek
llm = ChatOpenAI(model='deepseek-chat', api_key='你的key', base_url='https://api.deepseek.com/v1')

# 通义千问（阿里云）
llm = ChatOpenAI(model='qwen-plus', api_key='你的key', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')

# OpenAI
llm = ChatOpenAI(model='gpt-4o', api_key='你的key')
```

### 第六步：配置任务（无需修改）

编辑 `task.md` 文件，描述你希望 Agent 执行的任务。项目已包含默认任务模板。

关键配置项：
- **`n = 3`**：修改要下载的图片数量
- **搜索关键词**：修改 `'buddhist temple'` 为你想搜索的内容
- **目标网站**：修改 URL 为目标网站

### 第七步：配置 Information.md（可选）（无需修改）

如果需要使用 `extract_page_to_markdown` 工具提取网页内容，编辑 `Information.md`：

````markdown
```html
要提取内容的起始HTML标签或JS变量
要提取内容的结束HTML标签或分号
```
````

工具会在网页源码中查找匹配首尾行之间的内容并保存。

## 运行项目

```bash
# 确保虚拟环境已激活
# Windows:
.venv\Scripts\activate

# 运行主程序
python main.py
```

**运行流程：**
1. 读取 `task.md` 任务描述
2. 运行 `move_images.py` 清理 `image/` 目录（旧文件移入 `history/`）
3. 启动浏览器，创建 Agent
4. Agent 自动执行任务（搜索、下载、提取信息）
5. 运行 `rename_images.py` 根据 `title.txt` 重命名图片
6. 输出统计结果

**运行中操作：**
- 输入 `quit` + 回车 → 安全停止 Agent
- `Ctrl+C` → 强制中断

## 运行测试

```bash
# 不调用 LLM，验证代码功能
python test_main.py
```

测试覆盖：脚本执行、路径配置、工具注册、退出机制、文件验证、重命名逻辑等 39 个断言。

## 自定义工具说明

### extract_page_to_markdown

从当前网页源码中提取符合 `Information.md` 模式的内容。

**参数：**
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `output_filename` | `page_content.md` | 输出文件名 |
| `output_dir` | `image/` | 输出目录 |
| `format_type` | `markdown` | 格式：markdown / json / text |
| `information_file_path` | `Information.md` | 模式配置文件 |

**工作原理：**
1. 读取 `Information.md` 中的 HTML 代码块首尾行
2. 在网页源码中用 JavaScript 正则匹配
3. 将匹配内容保存为文件

## 各文件说明

| 文件 | 功能 |
|------|------|
| `main.py` | 主程序：读取任务、管理浏览器生命周期、运行 Agent、验证结果 |
| `tools_registry.py` | 注册自定义工具，包含路径安全验证 |
| `move_images.py` | 运行前清理：将 `image/` 中的文件移入 `history/` 的时间戳子文件夹 |
| `rename_images.py` | 运行后处理：读取 `title.txt`，将 `image_1.tiff` 等重命名为标题 |
| `task.md` | Agent 的任务指令，支持自定义目标网站、下载数量等 |
| `Information.md` | 自定义工具的 HTML 提取模式配置 |
| `test_main.py` | 功能测试脚本（不需要 LLM） |

## 常见问题

### Q: 浏览器没有自动打开？
确认已安装 Chrome 或 Chromium。程序会自动检测系统中的浏览器。

### Q: 下载的文件在哪里？
所有下载的图片保存在项目目录下的 `image/` 文件夹。

### Q: 如何修改下载数量？
编辑 `task.md` 文件，修改 `n = 3` 为你需要的数量。

### Q: LLM 报错 403 Forbidden？
如果使用 `ChatBrowserUse()`，免费账户不支持 LLM Gateway。请换用自己的 LLM API。

### Q: 如何更换目标网站？
编辑 `task.md`，修改目标 URL 和搜索策略。如需提取网页信息，同步更新 `Information.md` 中的 HTML 模式。

### Q: 重命名失败？
检查 `browseruse_agent_data/title.txt` 是否存在且格式正确（每行一个标题，最后一行为 `END`）。

## 技术架构

```
用户 → task.md → main.py → Agent (LLM + Browser)
                              ├── 浏览器操作（点击、下载）
                              ├── 自定义工具（tools_registry.py）
                              └── 文件系统（write_file, read_file）
                           → rename_images.py → 完成
```

## 许可证

本项目基于 [browser-use](https://github.com/browser-use/browser-use)（MIT License）。