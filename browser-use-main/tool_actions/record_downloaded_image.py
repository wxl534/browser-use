"""`record_downloaded_image` 工具：从 tools_registry.py 拆分而来。

共享 helper / 参数模型仍由 tools_registry 提供；运行时全局通过 tr.* 实时读取。
"""
import tools_registry as tr
from tools_registry import (
    ActionResult,
    Path,
    RecordDownloadedImageParams,
    _find_existing_image_file_by_hash,
    _hash_text,
    _load_image_records,
    _normalize_title,
    _prefix_from_filename,
    _record_file_sha256,
    _record_image_file_path,
    _record_sequence,
    _record_sort_key,
    _rename_image_to_final_name,
    _rewrite_image_info_file,
    _safe_agent_data_filename,
    _safe_record_sequence_for_existing_file,
    _sha256_file,
    _source_hash,
    _source_item_id_from_urls,
    _validate_saved_image_file,
    _write_image_records,
    datetime,
    timezone,
    tools,
)


@tools.action(
    description=(
        '记录一张已成功保存到 ImagesCache 缓存目录的非 LOC 图片。'
        '工具会用 UTF-8 自动去重并重写 browseruse_agent_data/image_record.jsonl 和信息表，'
        '避免 write_file 追加导致重复行或 GBK 编码错误。'
    ),
    param_model=RecordDownloadedImageParams,
)
async def record_downloaded_image(params: RecordDownloadedImageParams):
    """
    为普通网站/截图下载流程记录图片、标题和元数据。
    """
    try:
        data_dir = tr.AGENT_DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)

        image_path = _record_image_file_path(params.file_name)
        if not image_path.exists() or not image_path.is_file():
            return ActionResult(error=f'图片文件不存在，不能记录: {image_path}')
        try:
            _validate_saved_image_file(image_path, source='record_downloaded_image')
        except RuntimeError as exc:
            return ActionResult(error=f'图片质量校验失败，拒绝记录: {exc}')

        sequence, sequence_note = _safe_record_sequence_for_existing_file(
            params.sequence,
            params.record_filename,
            _prefix_from_filename(params.file_name, 'temple'),
            image_path,
        )
        file_hash = _sha256_file(image_path)
        normalized_title = _normalize_title(params.title, fallback=image_path.stem)
        source_hash = _source_hash(params.page_url, params.image_url, 0)
        record_file = data_dir / _safe_agent_data_filename(params.record_filename, 'image_record.jsonl')
        records = _load_image_records(record_file)
        for record in records:
            if record.get('status') != 'downloaded':
                continue
            if _record_sequence(record) == sequence:
                return ActionResult(error=f'序号 #{params.sequence} 已有记录，拒绝覆盖旧记录: {record.get("file_name", "")}')
            if Path(str(record.get('file_name') or '')).name == image_path.name:
                return ActionResult(error=f'文件名已被记录，拒绝覆盖旧记录: {image_path.name}')
            if source_hash and str(record.get('source_hash') or '').strip() == source_hash:
                image_path.unlink(missing_ok=True)
                msg = (
                    f'✅ 来源已处理，已删除本次重复文件并跳过\n'
                    f'- 已有序号: {record.get("sequence")}\n'
                    f'- 已有文件: {record.get("file_name", "")}\n'
                    f'- source_hash: {source_hash}'
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory=f'来源已记录，跳过重复记录: {record.get("file_name", "")}',
                )
            if params.image_url.strip() and str(record.get('image_url') or '').strip() == params.image_url.strip():
                image_path.unlink(missing_ok=True)
                msg = (
                    f'✅ 图片 URL 已有下载记录，已删除本次重复文件并跳过\n'
                    f'- 已有序号: {record.get("sequence")}\n'
                    f'- 已有文件: {record.get("file_name", "")}\n'
                    f'- 图片 URL: {params.image_url.strip()}'
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory=f'图片 URL 已记录，跳过重复记录: {record.get("file_name", "")}',
                )
            if _record_file_sha256(record) == file_hash:
                image_path.unlink(missing_ok=True)
                msg = (
                    f'✅ 图片内容已有下载记录，已删除本次重复文件并跳过\n'
                    f'- 已有序号: {record.get("sequence")}\n'
                    f'- 已有文件: {record.get("file_name", "")}\n'
                    f'- SHA256: {file_hash}'
                )
                return ActionResult(
                    extracted_content=msg,
                    include_in_memory=True,
                    long_term_memory=f'图片内容已记录，跳过重复记录: {record.get("file_name", "")}',
                )

        existing_image_path = _find_existing_image_file_by_hash(file_hash, exclude_path=image_path)
        if existing_image_path:
            image_path.unlink(missing_ok=True)
            msg = (
                f'✅ image 目录中已存在相同图片内容，已删除本次重复文件并跳过\n'
                f'- 已有文件: {existing_image_path.name}\n'
                f'- 本次文件: {image_path.name}\n'
                f'- SHA256: {file_hash}'
            )
            return ActionResult(
                extracted_content=msg,
                include_in_memory=True,
                long_term_memory=f'image 目录已有相同图片，跳过重复记录: {existing_image_path.name}',
            )

        embed_hash = source_hash or file_hash
        image_path = _rename_image_to_final_name(image_path, normalized_title, sequence, embed_hash)
        final_file_hash = _sha256_file(image_path)
        if final_file_hash != file_hash:
            raise RuntimeError(f'最终命名后图片 hash 变化: before={file_hash}, after={final_file_hash}')

        record = {
            'status': 'downloaded',
            'sequence': sequence,
            'file_name': image_path.name,
            'file_path': str(image_path),
            'file_size': image_path.stat().st_size,
            'sha256': file_hash,
            'content_hash': file_hash,
            'short_hash': file_hash[:8],
            'source_hash': source_hash,
            'source_item_id': _source_item_id_from_urls(params.page_url, params.image_url),
            'title_hash': _hash_text(normalized_title, 'sha1'),
            'title': normalized_title,
            'collection_title': _normalize_title(params.collection_title, fallback=params.title),
            'page_url': params.page_url.strip(),
            'image_url': params.image_url.strip(),
            'evidence': params.evidence.strip(),
            'metadata': params.metadata.strip(),
            'summary': params.summary.strip(),
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        }
        records.append(record)
        records.sort(key=_record_sort_key)

        _write_image_records(record_file, records)
        info_file = _rewrite_image_info_file(data_dir, records, params.info_filename)

        downloaded_count = sum(1 for item in records if item.get('status') == 'downloaded')
        msg = (
            f'✅ 已最终命名并记录图片 #{sequence}: {image_path.name}\n'
            f'{sequence_note + chr(10) if sequence_note else ""}'
            f'- content_hash: {file_hash}\n'
            f'- source_hash: {source_hash}\n'
            f'- 当前有效记录: {downloaded_count}\n'
            f'- 信息表: {info_file}\n'
            f'- 结构化记录: {record_file}'
        )
        return ActionResult(
            extracted_content=msg,
            include_in_memory=True,
            long_term_memory=f'已记录图片 #{params.sequence} {image_path.name}，当前共 {downloaded_count} 条有效记录',
        )
    except Exception as e:
        return ActionResult(error=f'记录图片时出错: {str(e)}')
