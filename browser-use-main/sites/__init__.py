"""可插拔站点模块（site plugins）。

每个站点把自己的 URL 判定/推导逻辑、进度 helper、参数模型集中在一个文件里，
通过 tools_registry.register_download_site_hint 把能力注入通用下载/发号工具。
tools_registry 通用核心因此不含任何站点硬编码；新增站点只需在此目录加一个模块。
"""
