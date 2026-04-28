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

**⚠️ 每张图片只需 3 步：**
1. Step 1: 点击图片进入详情页 + **调用 extract_page_to_markdown 工具**（以照片标题命名）
2. Step 2: 选择 TIFF 格式 + 点击 Go 下载
3. Step 3: 点击 "Back to Search Results" 返回

**必须使用的自定义工具：**
- **extract_page_to_markdown**: 从当前网页提取符合 Information.md 中 HTML 代码块首尾行的内容，保存为文件
  ```
  使用方法：调用 extract_page_to_markdown 工具
  参数：
    output_filename: "照片标题.md"  （使用照片标题命名）
  其余参数使用默认值即可（output_dir 默认为 image 目录，information_file_path 默认为 Information.md）
  ```
- **工具说明**：
  - 自动读取 Information.md 中定义的 HTML 代码块首尾行模式
  - 在当前网页源代码中查找匹配的 HTML 片段
  - 将匹配内容保存为 Markdown/JSON/Text 文件
  - 需要先在 Information.md 中配置好要提取的 HTML 模式

**识别下载方式**
   - 检查页面，找到下载按钮，寻找一个向下箭头的下拉选择框
   - 选择 TIFF 格式（如果有）
   - 点击 "Go" 按钮开始下载，只用点击一次，无论你认为是否成功不用进行重试
   - 如果没有 TIFF 选项，直接跳过该图片，返回继续下一张

**⚠️ 重要优化指令：**
- 不要在每张图片处理时写入 title.txt（最后统一写）
- 不要验证下载是否成功（下载会自动完成）
- **完全禁用截图功能**：不截图、不等待截图超时
- 使用 JavaScript 提取页面内容，比视觉识别更快速准确

**重复以上步骤直到处理完 n 张图片**

### 阶段 3 - 记录 title
1. **所有图片处理完成后，统一写入 title.txt**
   - 将所有识别到的 title 按顺序写入
   - 使用 write_file 工具写入到 browseruse_agent_data/title.txt

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
   - 最后必须加上 "END" 标记

## 工具与脚本说明

### browser-use 内置功能
- **文件下载**：自动识别下载链接或使用图片 URL 下载
- **JavaScript 执行**：用于分析 DOM 并提取图片 title
- **文件写入**：使用 write_file 动作将 title 列表保存到 browseruse_agent_data/title.txt
- **页面导航**：click、go_to_url、navigate_back 等操作

### 自定义工具: extract_page_to_markdown（主要工具）
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

## 错误处理

### 工具执行失败（重要）
- 如果 extract_page_to_markdown 工具失败：
  - **不要停止**，尝试手动使用 JavaScript 提取页面信息
  - 记录失败的图片，继续处理下一张
  - **不要连续失败 3 次就放弃**，必须处理完所有 n 张图片

### 网页打开失败
- 如果网页打开失败，如等待超时，不要停止程序，退回到上一个页面继续处理下一张图片

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
- 📊 **最后汇总**: 所有图片处理完成后，统一写入 title.txt
- 💾 **关键流程**: 进入详情页 → extract_page_to_markdown(提取页面内容) → 选择 TIFF → 下载 → 返回 → 继续下一张
- 🔧 **技术配置**: use_vision=False，完全关闭视觉识别和截图功能
- 📝 **工具说明**: extract_page_to_markdown 需要配合 Information.md 使用，确保已配置好提取模式
