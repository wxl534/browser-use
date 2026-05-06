# 图片下载任务配置

## 任务目标
搜索并下载 n = 3 张 'buddhist temple' 相关图片到本地 image 目录，自动识别图片下载链接并下载，使用网页中的图片标题命名，并使用工具提取图片信息。

## 执行流程概览
1. **搜索识别**：访问 https://www.loc.gov，通过人机识别，搜索 'buddhist temple'
2. **筛选结果**：点击 'Photo, Print, Drawing' 筛选可下载图片
3. **批量处理**：对每张图片执行以下操作：
   - 点击图片进入详情页
   - **调用 extract_page_to_markdown 工具**提取页面信息（使用默认参数即可，可指定 output_filename）
   - 选择 TIFF 格式并点击下载
   - 返回搜索结果页继续下一张
4. **保存汇总**：处理完成后将所有 title 写入 title.txt，用于后续重命名

## 详细执行步骤

### 阶段 1 - 搜索与识别
1. **访问网站 https://www.loc.gov**

2. **搜索关键词：'buddhist temple'**
   - 在搜索框输入关键词并提交
   - 验证搜索成功：检查页面中是否有图片结果

### 阶段 2 - 下载图片（核心功能）
**对每张图片（共 n 张）执行以下操作：**

**⚠️ 每张图片只需 4 步：**
1. Step 1: 点击图片进入详情页 + **调用 extract_page_to_markdown 工具**（以照片标题命名）
2. Step 2: **调用 select_download_format 工具**选择 TIFF 格式并自动点击 Go 下载
3. Step 3: **立即将该图片的 title 追加写入 browseruse_agent_data/title.txt**
4. Step 4: 点击 "Back to Search Results" 返回

**必须使用的自定义工具：**
- **extract_page_to_markdown**: 从当前网页提取符合 Information.md 中 HTML 代码块首尾行的内容，保存为文件
  ```
  使用方法：调用 extract_page_to_markdown 工具
  参数：
    output_filename: "照片标题.md"  （使用照片标题命名）
  ⚠️ 其余参数必须使用默认值！不要传入 output_dir、information_file_path 参数！
  ⚠️ 绝对禁止传入绝对路径或相对路径如 "../Information.md" 或 "/C:/Users/..."
  ```
- **select_download_format**: 自动找到页面中的下载格式选择器，选择指定格式并点击 Go 下载
  ```
  使用方法：调用 select_download_format 工具
  参数：
    preferred_format: "TIFF"  （默认值，通常不需要修改）
  ```
  - ⚠️ **必须使用此工具代替手动操作下拉框**，因为页面下拉框选项文本含特殊字符，select_dropdown 无法正确匹配
  - 工具会自动通过 JavaScript 操作 `<select id="select-resource0">` 元素
  - 如果没有 TIFF 选项，工具会返回所有可用格式列表，可据此选择其他格式
  - 工具会自动点击 Go 按钮，无需手动点击
  - 🚫 **绝对禁止用 evaluate 执行此工具！** 它是注册的 action 工具，必须像 extract_page_to_markdown 一样直接调用，不是 JavaScript 函数！
  - ✅ 正确方式：`select_download_format(preferred_format="TIFF")` 作为工具 action 调用
  - ❌ 错误方式：`evaluate(code="select_download_format(preferred_format='TIFF')")` — 这会导致 JS 报错

- **工具说明（extract_page_to_markdown）**：
  - 自动读取 Information.md 中定义的 HTML 代码块首尾行模式
  - 在当前网页源代码中查找匹配的 HTML 片段
  - 将匹配内容保存为 Markdown/JSON/Text 文件
  - 需要先在 Information.md 中配置好要提取的 HTML 模式

**下载流程（使用 select_download_format 工具）**
   - 进入详情页后，**直接调用 select_download_format(preferred_format="TIFF")** 作为工具 action
   - ⚠️ **调用方式必须与 extract_page_to_markdown 完全一致** — 作为注册的工具 action 调用，不是 evaluate/JavaScript
   - 工具会自动完成：查找选择器 → 选择 TIFF → 点击 Go 下载
   - 如果工具返回"未找到格式: TIFF"，说明该图片没有 TIFF 选项，直接跳过返回继续下一张
   - **不要使用 select_dropdown 或手动点击下拉框**，直接用此工具
   - **不要使用 evaluate 执行此工具**，它不是浏览器 JavaScript 函数

**⚠️ 重要优化指令：**
- **每处理完一张图片，立即将 title 追加写入 title.txt**（只传文件名 "title.txt"，不传路径）
- **调用 extract_page_to_markdown 时只传 output_filename 参数**，其他参数不要传
- 不要验证下载是否成功（下载会自动完成）
- **完全禁用截图功能**：不截图、不等待截图超时
- 使用 JavaScript 提取页面内容，比视觉识别更快速准确

**重复以上步骤直到处理完 n 张图片**

### 阶段 3 - 记录 title（每张图片处理后立即写入）
1. **每处理完一张图片，立即追加写入 title.txt**
   - 使用 write_file 工具，**file_path 只传文件名 "title.txt"**（不要传完整路径！）
   - ⚠️ 错误写法：`file_path: "C:/Users/.../title.txt"` — 会导致文件找不到
   - ✅ 正确写法：`file_path: "title.txt"`
   - 第一张图片写入时创建文件，后续追加
   - **所有 n 张图片处理完成后，在文件末尾追加 "END" 标记**

2. **输出格式要求**：
   ```
   [标题 1]
   [标题 2]
   ...
   [标题 n]
   END
   ```

3. **注意事项**：
   - 必须换行输出，不能使用 `\n` 字符
   - title 顺序必须与下载顺序严格对应
   - 最后一张图片处理完后，追加 "END" 标记
   - 如果某张图片被跳过（无 TIFF），不写入该图片的 title

## 工具与脚本说明

### browser-use 内置功能
- **文件下载**：自动识别下载链接或使用图片 URL 下载
- **JavaScript 执行**：用于分析 DOM 并提取图片 title
- **文件写入**：使用 write_file 动作将 title 列表保存到 browseruse_agent_data/title.txt
- **页面导航**：click、go_to_url、navigate_back 等操作

### 自定义工具: extract_page_to_markdown（页面提取工具）
- **功能**: 使用 JavaScript 从当前网页提取符合 Information.md 中 HTML 代码块首尾行的内容，保存为文件
- **工作流程**:
  1. 进入照片详情页
  2. 调用 extract_page_to_markdown 工具（指定 output_filename，其余用默认值）
  3. 工具自动执行：
     - 读取 Information.md 中的 HTML 代码块模式
     - 在页面源代码中查找匹配内容
     - 保存到 image 目录
  4. 继续下载流程
- **前提条件**: 需要先在 Information.md 中配置好 HTML 代码块模式（用 ```html ... ``` 包裹首尾行）

### 自定义工具: select_download_format（下载格式选择工具）
- **功能**: 通过 JavaScript 自动操作 LOC 网站的下载格式 `<select>` 元素，选择指定格式并点击 Go 下载
- **为什么需要此工具**: 页面下拉框选项文本包含 `&nbsp;`（不间断空格），导致 browser-use 内置的 `select_dropdown` 无法匹配选项
- **工作流程**:
  1. 在页面中查找 `id` 以 `select-resource` 开头的 `<select>` 元素
  2. 通过 `data-file-download` 属性精准匹配目标格式（不依赖文本内容）
  3. 设置 `selectedIndex` 并触发 `change` 事件
  4. 自动找到并点击旁边的 Go 按钮
- **调用方式**: `select_download_format(preferred_format="TIFF")`
- **返回结果**:
  - 成功：返回已选择的格式和下载 URL
  - 失败：返回错误信息和所有可用格式列表（可据此重新选择）

## 错误处理

### 工具执行失败（重要）
- 如果 extract_page_to_markdown 工具失败：
  - **不要停止**，尝试手动使用 JavaScript 提取页面信息
  - 记录失败的图片，继续处理下一张
  - **不要连续失败 3 次就放弃**，必须处理完所有 n 张图片

### 下载格式选择失败
- 如果 select_download_format 返回"未找到格式: TIFF"：
  - 查看返回的可用格式列表，如果有其他格式（如 JPEG）可尝试选择
  - 如果完全没有下载选项（select-resource 不存在），直接跳过该图片
  - **不要回退使用 select_dropdown 或手动操作下拉框**

### 页面元素定位失败（重要！）
- 如果点击某个元素报错 "Element index not available" 或 "invalid element index"：
  - **不要反复尝试同一个 index**
  - 先使用 `scroll(down=True, pages=1)` 不带 index 参数来滚动页面
  - 滚动后页面元素会重新编号，使用新的 index
  - 如果连续 2 次定位失败，使用 `navigate(url="当前搜索结果页URL")` 刷新页面
- 如果反复点击到已处理过的图片：
  - 记住已处理图片的标题，通过标题文字区分
  - 使用 `scroll(down=True, pages=2)` 向下滚动让新图片出现
  - 或直接点击 "Next" 翻到下一页搜索结果

### 网页打开失败
- 如果网页打开失败，如等待超时，不要停止程序，退回到上一个页面继续处理下一张图片

### 通用错误恢复策略（核心原则）
- ⚠️ **无论遇到什么错误，绝对不要调用 done 终止任务**
- 遇到任何错误时：跳过当前图片 → 返回搜索结果 → 继续下一张
- 如果当前页面状态混乱，使用 navigate 回到搜索结果页的 URL 重新开始
- 只有处理完所有 n 张图片后，才能调用 done 结束任务

### 标题提取失败
- 如果无法找到标题，使用 URL 中的文件名作为替代
- 在 title.txt 中记录为占位符（如 `image_1`, `image_2`）

### 重命名失败
- 如果 title.txt 不存在，脚本会提示错误
- 如果图片数量与 title 数量不匹配，只处理较小数量的那一方
- 如果文件名冲突，自动添加序号后缀（如 `title_1.png`, `title_2.png`）

## 动态内容处理
- 🔄 如果页面使用懒加载，先滚动到页面底部
- ⏱️ 等待所有图片加载完成（检查网络活动静止）
- 📄 如果图片数量不足 n，尝试翻页或加载更多
- 🔍 尝试不同的下载方式（直接下载、右键另存为等）

## 重要提醒
- **核心策略**: 使用 extract_page_to_markdown 工具提取页面内容，结合下载功能完成任务
- 📁 **保存位置**:
  - 图片自动下载到 image 目录
  - 提取的网页内容也保存到 image 目录（仅存档）
  - **标题列表保存到 browseruse_agent_data/title.txt**（用于重命名）
- ⚡ **高效处理**: 每张图片只需 3 步（进入+提取 → 选择下载 → 返回）
- ⏱️ **跳过验证**: 不验证下载、**完全禁用截图**、不等待太久
- 🔄 **跳过无权限**: 没有 TIFF 选项的图片直接跳过
- 📊 **即时写入**: 每张图片处理完立即追加 title 到 title.txt，最后追加 END 标记
- 💾 **关键流程**: 进入详情页 → extract_page_to_markdown(提取页面内容) → select_download_format(选择TIFF并下载) → 追加title到title.txt → 返回 → 继续下一张
- 🔧 **技术配置**: use_vision=False，完全关闭视觉识别和截图功能
- 📝 **工具说明**: extract_page_to_markdown 需要配合 Information.md 使用，确保已配置好提取模式
- 🚫 **禁止操作**: 不要使用 select_dropdown、click 下拉框、evaluate 等方式选择下载格式，必须使用 select_download_format 工具作为 action 调用（与 extract_page_to_markdown 调用方式一致）
