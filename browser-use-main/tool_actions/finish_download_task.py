"""`finish_download_task` 工具：从 tools_registry.py 拆分而来。

共享 helper / 参数模型仍由 tools_registry 提供；运行时全局通过 tr.* 实时读取。
"""
import tools_registry as tr
from tools_registry import (
    ActionResult,
    FinishDownloadTaskParams,
    format_download_validation_report,
    tools,
    validate_download_artifacts,
)


@tools.action(
    description=(
        '用本地文件的确定性校验报告结束任务；不要再调用内置 done。'
        '校验 SUCCESS 时返回 success=True；否则返回 success=False，最终文本只包含程序报告，避免 LLM 自行扩写乱码。'
    ),
    param_model=FinishDownloadTaskParams,
)
async def finish_download_task(params: FinishDownloadTaskParams):
    validation = validate_download_artifacts(
        target_count=params.target_count,
        record_filename=params.record_filename,
        title_filename=params.title_filename,
        validate_image_files=True,
        include_duplicate_hash_groups=True,
    )
    report = format_download_validation_report(validation)
    report_file = tr.AGENT_DATA_DIR / 'final_download_report.md'
    report_file.write_text(report + '\n', encoding='utf-8')
    return ActionResult(
        is_done=True,
        success=bool(validation['complete']),
        extracted_content=report,
        long_term_memory=(
            'Task finished with deterministic validation SUCCESS'
            if validation['complete']
            else f'Task finished with deterministic validation INCOMPLETE; need {validation["remaining_records"]} more valid records'
        ),
    )
