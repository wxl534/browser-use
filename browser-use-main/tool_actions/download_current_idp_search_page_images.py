"""`download_current_idp_search_page_images` 工具：从 tools_registry.py 拆分而来。

共享 helper / 参数模型仍由 tools_registry 提供；运行时全局通过 tr.* 实时读取。
"""
import tools_registry as tr
from tools_registry import (
    DownloadCurrentIdpSearchPageImagesParams,
    IDPAdapter,
    run_search_page_batch,
    tools,
)


@tools.action(
    description=(
        '批量处理当前 IDP 搜索结果页：一次提取多个 /collection/ 藏品，'
        '在浏览器上下文 fetch IIIF manifest 和图片，下载到 image 目录，并写入 image_record.jsonl/title.txt。'
        '用于替代 agent 每张图逐页点击，可显著提升 china temple 这类 IDP 批量任务效率。'
    ),
    param_model=DownloadCurrentIdpSearchPageImagesParams,
)
async def download_current_idp_search_page_images(params: DownloadCurrentIdpSearchPageImagesParams, browser_session):
    return await run_search_page_batch(
        IDPAdapter(),
        params=params,
        browser_session=browser_session,
    )
