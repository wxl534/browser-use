"""`validate_download_completion` 工具:从 tools_registry.py 拆分而来.

共享 helper / 参数模型仍由 tools_registry 提供;运行时全局通过 tr.* 实时读取.
"""
import tools_registry as tr
from tools_registry import (
    ActionResult,
    ValidateDownloadCompletionParams,
    format_download_validation_report,
    tools,
    validate_download_artifacts,
)


@tools.action(
    description='最终校验下载结果,只根据 image_record.jsonl 和 ImagesCache 缓存目录生成确定性报告;done 前必须先调用它,不要让 agent 自己编统计.',
    param_model=ValidateDownloadCompletionParams,
)
async def validate_download_completion(params: ValidateDownloadCompletionParams):
    validation = validate_download_artifacts(
        target_count=params.target_count,
        record_filename=params.record_filename,
        validate_image_files=True,
        include_duplicate_hash_groups=True,
    )
    report = format_download_validation_report(validation)
    report_file = tr.AGENT_DATA_DIR / 'final_download_report.md'
    report_file.write_text(report + '\n', encoding='utf-8')
    return ActionResult(
        extracted_content=report,
        include_in_memory=True,
        long_term_memory=(
            'Final validation passed; finish_download_task may end with success=True'
            if validation['complete']
            else f'Final validation incomplete; need {validation["remaining_records"]} more valid records; do not finish yet'
        ),
    )
