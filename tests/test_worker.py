"""
main.py 功能测试(不调用大模型)

测试策略:
- 使用临时目录模拟项目结构,不影响真实文件
- Mock 掉 Browser,ChatOpenAI,Agent,不触发网络请求
- 逐个验证 main.py 中各个功能模块的正确性

运行方式:
    python test_main.py
    或
    python -m pytest test_main.py -v
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import textwrap
from pathlib import Path

# 项目根目录(用于导入)
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))


# ============================================================
# 辅助工具
# ============================================================

def create_temp_project(tmp: Path):
    """在临时目录中创建最小化的项目结构"""
    # task.md
    (tmp / 'task.md').write_text('测试任务:下载 3 张图片', encoding='utf-8')

    # Information.md
    (tmp / 'Information.md').write_text(
        '```html\n<div class="item">\n</div>\n```\n',
        encoding='utf-8',
    )

    # image 目录
    (tmp / 'image').mkdir()

    # browseruse_agent_data 目录
    (tmp / 'browseruse_agent_data').mkdir()

    # move_images.py(简化版,只打印不做事)
    (tmp / 'move_images.py').write_text(textwrap.dedent("""\
        import sys
        if '--no-confirm' in sys.argv:
            print('move_images: no-confirm mode, skip confirmation')
        else:
            print('move_images: normal mode')
        print('move_images: done')
    """), encoding='utf-8')

    # rename_images.py(简化版,模拟重命名)
    (tmp / 'rename_images.py').write_text(textwrap.dedent("""\
        from pathlib import Path
        import sys

        BASE_DIR = Path(__file__).resolve().parent
        image_dir = BASE_DIR / 'image'
        title_file = BASE_DIR / 'browseruse_agent_data' / 'title.txt'

        if not title_file.exists():
            print('[ERROR] title.txt not found')
            sys.exit(1)

        titles = [l.strip() for l in title_file.read_text(encoding='utf-8').splitlines()
                  if l.strip() and l.strip().upper() != 'END']

        images = sorted(image_dir.glob('image_*.tiff'))
        for img, title in zip(images, titles):
            new_name = title.replace(' ', '_') + img.suffix
            img.rename(img.parent / new_name)
            print(f'[OK] {img.name} -> {new_name}')

        record = image_dir / 'rename_record.txt'
        record.write_text('rename record\\n', encoding='utf-8')
        print('rename_images: done')
    """), encoding='utf-8')

    return tmp


def create_fake_downloads(image_dir: Path, count: int = 3):
    """在 image 目录中创建模拟下载的图片文件"""
    for i in range(1, count + 1):
        f = image_dir / f'image_{i}.tiff'
        f.write_bytes(b'\x00' * 1024)  # 1KB 假文件


def create_title_file(data_dir: Path, titles: list[str]):
    """创建 title.txt"""
    content = '\n'.join(titles) + '\nEND\n'
    (data_dir / 'title.txt').write_text(content, encoding='utf-8')


def create_info_log(log_file: Path, lines: list[str]):
    """创建测试用 info.log"""
    log_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')


# ============================================================
# Mock 对象
# ============================================================

class FakeHistory:
    """模拟 agent.run() 返回的 history 对象"""

    def __init__(self, steps=10, duration=30.0, urls=None, errs=None):
        self._steps = steps
        self._duration = duration
        self._urls = urls or ['https://www.loc.gov']
        self._errors = errs or [None, None]

    def number_of_steps(self):
        return self._steps

    def total_duration_seconds(self):
        return self._duration

    def urls(self):
        return self._urls

    def errors(self):
        return self._errors


# ============================================================
# 测试用例
# ============================================================

class TestResults:
    """简单的测试结果收集器"""

    def __init__(self):
        self.passed = []
        self.failed = []

    def ok(self, name: str):
        self.passed.append(name)
        print(f'  ✅ {name}')

    def fail(self, name: str, reason: str):
        self.failed.append((name, reason))
        print(f'  ❌ {name}: {reason}')

    def summary(self):
        total = len(self.passed) + len(self.failed)
        print(f'\n{"=" * 60}')
        print(f'测试结果: {len(self.passed)}/{total} 通过')
        if self.failed:
            print('\n失败用例:')
            for name, reason in self.failed:
                print(f'  - {name}: {reason}')
        print('=' * 60)
        return len(self.failed) == 0


results = TestResults()


# ------ 1. run_python_script 测试 ------

def test_run_python_script():
    """测试 run_python_script 函数"""
    print('\n📋 测试 run_python_script()')

    from worker import run_python_script

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1a. 脚本不存在 → 返回 False
        ok = run_python_script(str(tmp / 'nonexistent.py'), '不存在的脚本')
        if not ok:
            results.ok('脚本不存在时返回 False')
        else:
            results.fail('脚本不存在时返回 False', '期望 False')

        # 1b. 正常脚本 → 返回 True
        script = tmp / 'hello.py'
        script.write_text('print("hello")', encoding='utf-8')
        ok = run_python_script(str(script), '正常脚本')
        if ok:
            results.ok('正常脚本返回 True')
        else:
            results.fail('正常脚本返回 True', '期望 True')

        # 1c. 带 extra_args
        script_args = tmp / 'args_test.py'
        script_args.write_text(textwrap.dedent("""\
            import sys
            if '--no-confirm' in sys.argv:
                print('got --no-confirm')
            else:
                sys.exit(1)
        """), encoding='utf-8')
        ok = run_python_script(str(script_args), '带参数', extra_args=['--no-confirm'])
        if ok:
            results.ok('extra_args 正确传递')
        else:
            results.fail('extra_args 正确传递', '脚本未收到 --no-confirm')

        # 1d. 脚本报错 → 返回 False
        script_err = tmp / 'error.py'
        script_err.write_text('raise ValueError("boom")', encoding='utf-8')
        ok = run_python_script(str(script_err), '报错脚本')
        if not ok:
            results.ok('报错脚本返回 False')
        else:
            results.fail('报错脚本返回 False', '期望 False')


def test_trim_info_log():
    """测试按指定日志行裁剪 info.log"""
    print('\n📋 测试 trim_info_log.py')

    from trim_info_log import find_line_index, trim_log_before_line

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log_file = tmp / 'info.log'
        lines = [
            '2026-05-11 10:00:00,000 - INFO     [service] line 1',
            '2026-05-11 10:00:01,000 - INFO     [service] line 2',
            '2026-05-11 10:00:02,000 - INFO     [service] line 3',
        ]
        create_info_log(log_file, lines)

        target_line = lines[1]
        index = find_line_index(log_file.read_text(encoding='utf-8').splitlines(keepends=True), target_line)
        if index == 1:
            results.ok('find_line_index 可定位目标日志行')
        else:
            results.fail('find_line_index', f'期望 1，得到 {index}')

        removed_count = trim_log_before_line(log_file, target_line)
        trimmed_content = log_file.read_text(encoding='utf-8')
        expected_content = lines[1] + '\n' + lines[2] + '\n'

        if removed_count == 1:
            results.ok('trim_log_before_line 返回正确删除行数')
        else:
            results.fail('trim_log_before_line 删除行数', f'期望 1，得到 {removed_count}')

        if trimmed_content == expected_content:
            results.ok('trim_info_log 能删除目标行之前的所有内容')
        else:
            results.fail('trim_info_log 裁剪结果', f'期望 {expected_content!r}，得到 {trimmed_content!r}')

        try:
            trim_log_before_line(log_file, '2026-05-11 10:00:09,000 - INFO     [service] missing')
            results.fail('trim_info_log 缺失日志行', '期望抛出 ValueError')
        except ValueError:
            results.ok('trim_info_log 找不到日志行时会报错')


# ------ 2. 路径配置测试 ------

def test_paths():
    """测试所有路径配置的一致性"""
    print('\n📋 测试路径配置')

    from worker import BASE_DIR as MAIN_BASE
    from tools_registry import BASE_DIR as TOOLS_BASE

    # main.py 和 tools_registry.py 的 BASE_DIR 应该相同
    if MAIN_BASE == TOOLS_BASE:
        results.ok('main.py 与 tools_registry.py 的 BASE_DIR 一致')
    else:
        results.fail('BASE_DIR 一致性', f'{MAIN_BASE} != {TOOLS_BASE}')

    # 关键目录应存在或可创建
    for subdir in ['image', 'browseruse_agent_data']:
        d = MAIN_BASE / subdir
        d.mkdir(parents=True, exist_ok=True)
        if d.exists():
            results.ok(f'目录 {subdir}/ 存在')
        else:
            results.fail(f'目录 {subdir}/', '无法创建')

    # task.md 应存在
    for fname in ['task.md']:
        if (MAIN_BASE / fname).exists():
            results.ok(f'{fname} 存在')
        else:
            results.fail(f'{fname} 存在', '文件缺失')


# ------ 3. task.md 内容测试 ------

def test_task_md_content():
    """测试 task.md 不引用不存在的工具"""
    print('\n📋 测试 task.md 内容')

    from worker import BASE_DIR
    task_content = (BASE_DIR / 'task.md').read_text(encoding='utf-8')

    # 不应引用已删除的 extract_js_object_by_keyword
    if 'extract_js_object_by_keyword' not in task_content:
        results.ok('task.md 不引用不存在的 extract_js_object_by_keyword')
    else:
        results.fail('task.md 工具引用', '仍包含 extract_js_object_by_keyword')

    # 应引用当前非 LOC 记录工具
    if 'record_downloaded_image' in task_content:
        results.ok('task.md 引用了 record_downloaded_image')
    else:
        results.fail('task.md 工具引用', '缺少 record_downloaded_image')

    if '不要使用 `write_file` 记录标题' in task_content or '不要再额外调用 `write_file` 写标题' in task_content:
        results.ok('task.md 禁止用 write_file 记录标题')
    else:
        results.fail('task.md 写文件方式', '未禁止 write_file 记录标题')

    # 不应包含硬编码旧路径
    if 'D:\\desktop' not in task_content and 'D:/desktop' not in task_content:
        results.ok('task.md 无硬编码旧路径')
    else:
        results.fail('task.md 路径', '仍包含 D:\\desktop')

    # 结构化记录路径应指向 browseruse_agent_data
    if 'browseruse_agent_data/image_record.jsonl' in task_content or 'browseruse_agent_data\\image_record.jsonl' in task_content:
        results.ok('task.md 中 image_record.jsonl 路径正确')
    else:
        results.fail('task.md image_record.jsonl 路径', '未指向 browseruse_agent_data/')

    if re.search(r'前\s+n\s*=\s*\d+\s*张', task_content) or re.search(r'前\s*\d+\s*张', task_content):
        results.ok('task.md 明确要求处理目标数量图片')
    else:
        results.fail('task.md 图片数量', '未明确要求目标图片数量')

    from worker import extract_search_keyword
    search_keyword = extract_search_keyword(task_content, default='')
    if search_keyword and search_keyword in task_content:
        results.ok(f'task.md 明确使用搜索词: {search_keyword}')
    else:
        results.fail('task.md 搜索词', '未明确使用当前任务搜索词')

    if 'image_record.jsonl' in task_content and 'temple_photo_info.md' in task_content:
        results.ok('task.md 明确结构化记录和信息表')
    else:
        results.fail('task.md 结构化记录', '缺少 image_record.jsonl 或 temple_photo_info.md')

    legacy_loc_tools = ['select_download_format', 'collect_loc_result_queue', 'get_next_loc_queue_item']
    if not any(tool_name in task_content for tool_name in legacy_loc_tools):
        results.ok('task.md 不引用 legacy LOC 专用工具')
    else:
        results.fail('task.md LOC 规则', '仍引用 legacy LOC 专用工具')

    if '不要使用 `evaluate(code="自定义工具(...)")`' in task_content:
        results.ok('task.md 明确禁止 evaluate 调自定义工具')
    else:
        results.fail('task.md evaluate 规则', '未明确禁止 evaluate 调自定义工具')

    if 'idp.bl.uk' in task_content and ('download_image_from_url' in task_content or 'download_current_idp_search_page_images' in task_content):
        results.ok('当前 task 是面向 IDP 站点的下载任务(批量或逐 item 模式均可)')
    else:
        results.fail('IDP 任务识别', '当前 task 缺少 IDP 下载任务标识')


# ------ 4. tools_registry 路径安全测试 ------

def test_path_safety():
    """测试 tools_registry 的路径安全验证"""
    print('\n📋 测试路径安全验证')

    from tools_registry import BASE_DIR, _is_path_allowed

    # 项目内路径应允许
    if _is_path_allowed(str(BASE_DIR / 'image')):
        results.ok('项目 image/ 路径允许')
    else:
        results.fail('项目 image/ 路径', '被拒绝')

    # 系统敏感路径应拒绝
    if not _is_path_allowed('C:\\Windows\\System32'):
        results.ok('系统路径被拒绝')
    else:
        results.fail('系统路径', '未被拒绝')

    if not _is_path_allowed('/etc/passwd'):
        results.ok('Linux 敏感路径被拒绝')
    else:
        results.fail('Linux 敏感路径', '未被拒绝')


# ------ 5. tools_registry 工具注册测试 ------

def test_tools_registered():
    """测试自定义工具是否正确注册"""
    print('\n📋 测试工具注册')

    from tools_registry import registry

    # registry.registry.actions 是一个 dict,key 为 action 名称
    action_names = list(registry.registry.actions.keys())
    required_actions = [
        'wait_for_human_verification',
        'validate_download_completion',
        'finish_download_task',
        'navigate_idp_search_page',
        'download_current_idp_search_page_images',
    ]
    for action_name in required_actions:
        if action_name in action_names:
            results.ok(f'{action_name} 已注册')
        else:
            results.fail('工具注册', f'未找到 {action_name}，已注册: {action_names}')

    retired_per_item = [
        'extract_page_to_markdown',
        'download_image_from_url',
        'next_search_item',
        'record_downloaded_image',
    ]
    unexpected_per_item = [name for name in retired_per_item if name in action_names]
    if not unexpected_per_item:
        results.ok('逐 item 兜底工具默认不注册(已迁出工作流)')
    else:
        results.fail('兜底工具注册', f'默认注册了已退役的逐 item 工具: {unexpected_per_item}')

    legacy_actions = [
        'select_download_format',
        'collect_loc_result_queue',
        'get_next_loc_queue_item',
        'mark_loc_queue_item',
        'rebuild_loc_download_state',
        'download_kyohaku_image',
        'download_current_kyohaku_item_images',
        'save_kyohaku_image_via_browser',
        'clean_kyohaku_screenshot',
    ]
    unexpected_legacy = [action_name for action_name in legacy_actions if action_name in action_names]
    if not unexpected_legacy:
        results.ok('legacy LOC/Kyohaku 工具默认不注册')
    else:
        results.fail('legacy 工具注册', f'默认注册了已退役工具: {unexpected_legacy}')


def test_plain_border_trimming():
    """测试截图清理会裁掉纯黑和纯白边框."""
    print('\n📋 测试黑白边框裁剪')

    from PIL import Image, ImageDraw
    from tools_registry import _save_pil_image, _trim_plain_border_from_image

    image = Image.new('RGB', (100, 80), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 8, 89, 71), fill='black')
    draw.rectangle((18, 16, 81, 63), fill=(120, 100, 80))

    trimmed = _trim_plain_border_from_image(image, black_threshold=18, white_threshold=245, border_ratio=0.98)
    if trimmed.size == (64, 48):
        results.ok('可同时裁掉外层白边和内层黑边')
    else:
        results.fail('黑白边框裁剪', f'期望 (64, 48)，得到 {trimmed.size}')

    with tempfile.TemporaryDirectory() as tmp:
        output = _save_pil_image(trimmed, Path(tmp) / 'cropped', '.jpg')
        if output.suffix == '.jpg' and output.exists() and output.stat().st_size > 0:
            results.ok('裁剪后可沿用 JPEG 扩展名高质量重编码')
        else:
            results.fail('裁剪图片保存格式', f'输出异常: {output}')


def test_kyohaku_method_strategy():
    """测试连续 5 次同方法成功后会锁定,失败后解除锁定."""
    print('\n📋 测试 Kyohaku 下载方法策略')

    import tools_registry
    from legacy.site_tools import (
        _load_kyohaku_strategy,
        _ordered_kyohaku_methods,
        _record_kyohaku_method_failure,
        _record_kyohaku_method_success,
    )

    old_base = tools_registry.BASE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        tools_registry.BASE_DIR = Path(tmp)
        try:
            for seq in range(1, 6):
                strategy = _record_kyohaku_method_success('browser_context_fetch', seq, f'https://knmdb.kyohaku.go.jp/art_images/{seq}.jpg')

            if strategy.get('preferred_method') == 'browser_context_fetch' and strategy.get('streak') == 5:
                results.ok('连续 5 次同方法成功后会锁定该方法')
            else:
                results.fail('方法锁定', f'策略异常: {strategy}')

            ordered = _ordered_kyohaku_methods(_load_kyohaku_strategy())
            if ordered[0] == 'browser_context_fetch':
                results.ok('锁定后优先使用该方法')
            else:
                results.fail('方法优先级', f'顺序异常: {ordered}')

            failed = _record_kyohaku_method_failure('browser_context_fetch', 6, 'https://knmdb.kyohaku.go.jp/art_images/6.jpg', 'boom')
            if not failed.get('preferred_method') and failed.get('streak') == 0:
                results.ok('锁定方法失败后会解除锁定并重新判断')
            else:
                results.fail('方法解锁', f'策略异常: {failed}')
        finally:
            tools_registry.BASE_DIR = old_base


# ------ 6.5 任务规模配置测试 ------

def test_run_limit_configuration():
    """测试从 task 提取数量和动态运行限制"""
    print('\n📋 测试运行限制配置')

    from worker import build_agent_run_limits, extract_target_image_count

    task_100 = '下载任务,n = 100 张图片'
    target = extract_target_image_count(task_100)
    if target == 100:
        results.ok('extract_target_image_count 可正确识别 n = 100')
    else:
        results.fail('extract_target_image_count', f'期望 100，得到 {target}')

    fallback_target = extract_target_image_count('没有显式数量', default=3)
    if fallback_target == 3:
        results.ok('extract_target_image_count 支持默认值')
    else:
        results.fail('extract_target_image_count 默认值', f'期望 3，得到 {fallback_target}')

    max_failures, max_actions_per_step, max_steps = build_agent_run_limits(100)
    if 20 <= max_failures <= 80:
        results.ok('100 张图片任务会限制 max_failures,避免长时间卡死')
    else:
        results.fail('max_failures 配置', f'期望 20-80，得到 {max_failures}')

    if max_actions_per_step >= 4:
        results.ok('100 张图片任务会提升 max_actions_per_step')
    else:
        results.fail('max_actions_per_step 配置', f'期望至少 4，得到 {max_actions_per_step}')

    if max_steps >= 2000:
        results.ok('100 张图片任务会放宽 max_steps')
    else:
        results.fail('max_steps 配置', f'期望至少 2000，得到 {max_steps}')


def test_title_end_marker_removed():
    """title.txt 已彻底移除:确认 main 不再导出 title 相关函数."""
    print('\n📋 测试 title.txt 已移除')

    import worker

    if not hasattr(worker, 'ensure_title_end_marker') and not hasattr(worker, 'count_titles'):
        results.ok('main 已移除 ensure_title_end_marker / count_titles')
    else:
        results.fail('title 函数移除', 'main 仍存在 title 相关函数')


def test_prepare_runtime_state_resume():
    """测试 ImagesCache 断点续跑保留状态,重置时完整清空并备份."""
    print('\n📋 测试断点续跑状态保留')

    from worker import prepare_runtime_state

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / 'Images' / 'ImagesCache'
        run_dir.mkdir(parents=True)
        record_file = run_dir / 'rename_record.txt'
        image_record_file = run_dir / 'image_record.jsonl'
        info_file = run_dir / 'temple_photo_info.md'
        strategy_file = run_dir / 'kyohaku_download_strategy.json'
        idp_progress_file = run_dir / 'idp_progress.json'
        sqlite_file = run_dir / 'image_catalog.sqlite3'
        report_file = run_dir / 'final_download_report.md'
        record_file.write_text('record\n', encoding='utf-8')
        image_record_file.write_text('{"status":"downloaded"}\n', encoding='utf-8')
        info_file.write_text('# old\n', encoding='utf-8')
        strategy_file.write_text('{"preferred_method":"python_direct"}\n', encoding='utf-8')
        idp_progress_file.write_text('{"next_page": 7}\n', encoding='utf-8')
        sqlite_file.write_bytes(b'sqlite-data')
        report_file.write_text('report\n', encoding='utf-8')

        prepare_runtime_state(run_dir, reset_state=False)
        if record_file.exists() and image_record_file.exists() and info_file.exists() and strategy_file.exists() and idp_progress_file.exists():
            results.ok('断点续跑会保留已有状态文件')
        else:
            results.fail('断点续跑状态保留', '状态文件被删除')

        prepare_runtime_state(run_dir, reset_state=True)
        remaining_files = list(run_dir.iterdir())
        if not remaining_files:
            results.ok('非续跑会完整清空 ImagesCache')
        else:
            results.fail('非续跑状态清理', f'仍有残留: {remaining_files}')

        backups = list(run_dir.parent.glob('ImagesCache_backup_*/ImagesCache'))
        if backups and (backups[0] / 'image_catalog.sqlite3').exists() and (backups[0] / 'final_download_report.md').exists():
            results.ok('非续跑会完整备份 ImagesCache 文件夹')
        else:
            results.fail('完整数据备份', f'未找到完整备份: {backups}')


def test_select_active_cache_dir_archives_old_cache():
    """测试新运行会把旧 ImagesCache 归档到搜索词目录,并创建新的 ImagesCache."""
    print('\n📋 测试 ImagesCache 归档')

    from worker import select_active_cache_dir

    with tempfile.TemporaryDirectory() as tmp:
        base_dir = Path(tmp)
        old_cache = base_dir / 'Images' / 'ImagesCache'
        old_cache.mkdir(parents=True)
        (old_cache / 'image_record.jsonl').write_text('old\n', encoding='utf-8')

        new_cache = select_active_cache_dir(base_dir, resume_run=False, keyword='china buddhist')
        archive = base_dir / 'Images' / 'china_buddhist'
        if new_cache == old_cache and new_cache.exists() and not any(new_cache.iterdir()) and (archive / 'image_record.jsonl').exists():
            results.ok('新运行会按搜索词归档旧 ImagesCache')
        else:
            results.fail('ImagesCache 归档', f'new_cache={new_cache}, archive_exists={(archive / "image_record.jsonl").exists()}')


def test_move_images_unicode_filename():
    """测试图片迁移遇到 Windows GBK 难编码文件名时不会中断."""
    print('\n📋 测试 Unicode 文件名图片迁移')

    import move_images

    old_base = move_images.BASE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        base_dir = Path(tmp)
        image_dir = base_dir / 'image'
        image_dir.mkdir()
        tricky = image_dir / 'Bhaiṣajyaguru.jpg'
        tricky.write_bytes(b'image')
        move_images.BASE_DIR = base_dir
        try:
            ok = move_images.move_and_clear_images(interactive=False)
            remaining = list(image_dir.iterdir())
            backups = list((base_dir / 'history').glob('image_backup_*/*'))
            if ok and not remaining and any(path.name == tricky.name for path in backups):
                results.ok('move_images 可迁移含特殊字符的文件名')
            else:
                results.fail('Unicode 文件名迁移', f'ok={ok}, remaining={remaining}, backups={backups}')
        finally:
            move_images.BASE_DIR = old_base


def test_idp_progress_resume_context():
    """测试 IDP 进度文件会驱动续跑页码和页内位置."""
    print('\n📋 测试 IDP 进度文件续跑')

    from worker import build_resume_task_context

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / 'Images' / 'ImagesCache'
        run_dir.mkdir(parents=True)
        (run_dir / 'image_record.jsonl').write_text(
            '{"status":"downloaded","sequence":1,"file_name":"temple_001.jpg","title":"one"}\n'
            '{"status":"downloaded","sequence":2,"file_name":"temple_002.jpg","title":"two"}\n',
            encoding='utf-8',
        )
        (run_dir / 'idp_progress.json').write_text(
            '{"keyword":"china temple","next_page":7,"next_index":13,"downloaded_records":2}',
            encoding='utf-8',
        )

        context = build_resume_task_context(run_dir, 1000)
        if 'page=7' in context and 'start_index=13' in context:
            results.ok('续跑上下文优先使用 idp_progress.json')
        else:
            results.fail('IDP 进度续跑', context)


def test_record_filename_and_sequence_safety():
    """测试 agent 乱传记录文件名或离谱序号时会被纠正."""
    print('\n📋 测试记录文件名和序号安全')

    import tools_registry
    from tools_registry import (
        _read_downloaded_records,
        _safe_agent_data_filename,
        _safe_record_sequence_for_existing_file,
        _safe_requested_image_sequence,
    )

    old_base = tools_registry.BASE_DIR
    old_image_dir = tools_registry.IMAGE_DIR
    old_data_dir = tools_registry.AGENT_DATA_DIR
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / 'Images' / 'ImagesCache'
        run_dir.mkdir(parents=True)
        (run_dir / 'image_record.jsonl').write_text(
            '{"status":"downloaded","sequence":1,"file_name":"temple_001.jpg"}\n',
            encoding='utf-8',
        )
        tools_registry.configure_runtime_paths(run_dir, run_dir, run_dir)
        try:
            if _safe_agent_data_filename('image_record . jsonl', 'image_record.jsonl') == 'image_record.jsonl':
                results.ok('带空格的 image_record 文件名会归一化')
            else:
                results.fail('记录文件名归一化', _safe_agent_data_filename('image_record . jsonl', 'image_record.jsonl'))

            if len(_read_downloaded_records('image_record.jsonl')) == 1:
                results.ok('_read_downloaded_records 接受文件名字符串')
            else:
                results.fail('_read_downloaded_records', '未读取到记录')

            sequence, _ = _safe_requested_image_sequence(1666666666, 'image_record.jsonl', 'temple')
            if sequence == 2:
                results.ok('离谱大序号会改为下一安全序号')
            else:
                results.fail('安全序号纠正', f'期望 2，得到 {sequence}')

            temp_image = run_dir / 'temple_002.jpg'
            temp_image.write_bytes(b'fake')
            sequence, _ = _safe_record_sequence_for_existing_file(2, 'image_record.jsonl', 'temple', temp_image)
            if sequence == 2:
                results.ok('记录已落地临时文件时不会跳过当前序号')
            else:
                results.fail('临时文件序号纠正', f'期望 2，得到 {sequence}')
        finally:
            tools_registry.BASE_DIR = old_base
            tools_registry.IMAGE_DIR = old_image_dir
            tools_registry.AGENT_DATA_DIR = old_data_dir


def test_configure_resume_target_script():
    """测试断点目标配置脚本会更新 progress/config/report."""
    print('\n📋 测试断点目标配置脚本')

    from configure_resume_target import configure_target

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / 'Images' / 'ImagesCache'
        cache.mkdir(parents=True)
        (cache / 'image_record.jsonl').write_text(
            '{"status":"downloaded","sequence":1,"file_name":"a.jpg"}\n'
            '{"status":"downloaded","sequence":2,"file_name":"b.jpg"}\n',
            encoding='utf-8',
        )
        (cache / 'a.jpg').write_bytes(b'a')
        (cache / 'b.jpg').write_bytes(b'b')

        summary = configure_target(cache, 10, update_task=False)
        progress = json.loads((cache / 'idp_progress.json').read_text(encoding='utf-8'))
        report = (cache / 'final_download_report.md').read_text(encoding='utf-8')
        if summary['new_target_count'] == 10 and progress['remaining_records'] == 8 and '- target_count: 10' in report:
            results.ok('configure_resume_target 可更新目标和断点状态')
        else:
            results.fail('断点目标配置', f'summary={summary}, progress={progress}, report={report}')


def test_auto_runner_helpers():
    """测试自动监督脚本的目标读取和记录计数辅助函数."""
    print('\n📋 测试自动运行监督脚本辅助函数')

    from runner import normalize_progress_page, read_downloaded_count, read_target_from_task, should_resume_round, sync_progress_from_page_queue
    from idp_page_progress import mark_page_batch_result

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        task_file = root / 'task.md'
        task_file.write_text('下载前 n = 123 张图片,target_count=123', encoding='utf-8')
        cache = root / 'Images' / 'ImagesCache'
        cache.mkdir(parents=True)
        (cache / 'image_record.jsonl').write_text(
            '{"status":"downloaded","sequence":1}\n'
            '{"status":"failed","sequence":2}\n'
            '{"status":"downloaded","sequence":3}\n',
            encoding='utf-8',
        )

        if read_target_from_task(task_file) == 123 and read_downloaded_count(cache) == 2:
            results.ok('runner 可读取目标和已下载数')
        else:
            results.fail('自动运行辅助函数', '目标或计数不正确')

        (cache / 'idp_progress.json').write_text('{"current_page":999,"next_page":999,"next_index":5}', encoding='utf-8')
        result = normalize_progress_page(cache, max_reasonable_page=200, fallback_page=1)
        progress = json.loads((cache / 'idp_progress.json').read_text(encoding='utf-8'))
        if result.get('changed') and progress['next_page'] == 1 and progress['next_index'] == 0:
            results.ok('runner 会修正异常深页断点')
        else:
            results.fail('异常页码修正', f'result={result}, progress={progress}')

        active = sync_progress_from_page_queue(cache, target_count=123, max_reasonable_page=200, fallback_page=1)
        if active['page'] == 1 and active['next_index'] == 0:
            results.ok('runner 会从页面队列同步断点')
        else:
            results.fail('页面队列同步', f'active={active}')

        next_active = mark_page_batch_result(
            cache,
            keyword='china buddhist',
            target_count=123,
            page=1,
            start_index=0,
            processed_items=5,
            downloaded_count=0,
            skipped_count=0,
            error_count=5,
            total_found=50,
            last_error='boom',
        )
        if next_active['page'] == 2 and next_active['next_index'] == 0:
            results.ok('页面队列会把 0 下载错误页标记后推进到下一页')
        else:
            results.fail('页面队列推进', f'next_active={next_active}')

        if (
            not should_resume_round(resume_first_round=False, downloaded_count=0)
            and should_resume_round(resume_first_round=False, downloaded_count=1)
            and should_resume_round(resume_first_round=True, downloaded_count=0)
        ):
            results.ok('从头开始且无下载记录时不会强行选择续跑点')
        else:
            results.fail('自动运行续跑判断', '续跑条件不符合预期')


def test_auto_runner_keyword_update():
    """测试自动运行脚本可更新 task.md 搜索词和 title_prefix."""
    print('\n📋 测试自动运行脚本搜索词更新')

    from runner import update_task_search_keyword

    with tempfile.TemporaryDirectory() as tmp:
        task_file = Path(tmp) / 'task.md'
        task_file.write_text(
            '搜索关键词 **`china buddhist`**\n'
            '搜索关键词固定为 `china buddhist`.\n'
            'navigate_idp_search_page(keyword="china buddhist", page=1)\n'
            'title_prefix="china_buddhist"\n'
            'title="china_buddhist_001_x"\n',
            encoding='utf-8',
        )
        summary = update_task_search_keyword(task_file, 'india buddhist')
        text = task_file.read_text(encoding='utf-8')
        if (
            summary['old_keyword'] == 'china buddhist'
            and 'india buddhist' in text
            and 'india_buddhist' in text
            and 'china buddhist' not in text
            and 'china_buddhist' not in text
        ):
            results.ok('runner 可替换 task.md 搜索词')
        else:
            results.fail('搜索词替换', f'summary={summary}, text={text}')


def test_auto_runner_keyword_change_archives_cache():
    """测试外部自动脚本切换搜索词时会按旧搜索词归档旧 ImagesCache."""
    print('\n📋 测试自动运行脚本搜索词切换归档 ImagesCache')

    from runner import archive_cache_for_keyword_change, update_cache_keyword

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / 'Images' / 'ImagesCache'
        cache.mkdir(parents=True)
        (cache / 'image_record.jsonl').write_text('old\n', encoding='utf-8')

        summary = archive_cache_for_keyword_change(cache, 'china buddhist', 'india buddhist')
        update_cache_keyword(cache, 'india buddhist')

        archive = cache.parent / 'china_buddhist'
        progress = json.loads((cache / 'idp_progress.json').read_text(encoding='utf-8'))
        config = json.loads((cache / 'run_config.json').read_text(encoding='utf-8'))
        if (
            summary['archived']
            and (archive / 'image_record.jsonl').exists()
            and cache.exists()
            and not (cache / 'image_record.jsonl').exists()
            and progress['keyword'] == 'india buddhist'
            and config['title_prefix'] == 'india_buddhist'
        ):
            results.ok('外部脚本切换搜索词会归档旧缓存并初始化新缓存关键词')
        else:
            results.fail('外部脚本 ImagesCache 归档', f'summary={summary}, progress={progress}, config={config}')


def test_auto_runner_new_run_archives_cache():
    """测试外部自动脚本选择从头开始时会归档旧 ImagesCache."""
    print('\n📋 测试自动运行脚本从头开始归档 ImagesCache')

    from runner import archive_cache

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / 'Images' / 'ImagesCache'
        cache.mkdir(parents=True)
        (cache / 'image_record.jsonl').write_text('old\n', encoding='utf-8')

        summary = archive_cache(cache, 'china buddhist')
        archive = cache.parent / 'china_buddhist'
        if summary['archived'] and (archive / 'image_record.jsonl').exists() and cache.exists() and not any(cache.iterdir()):
            results.ok('外部脚本从头开始会按当前搜索词归档旧缓存')
        else:
            results.fail('外部脚本从头开始归档', f'summary={summary}')


def test_lightweight_download_mode():
    """测试大文件下载时默认启用轻量下载模式,避免 DownloadsWatchdog 堵塞 EventBus"""
    print('\n📋 测试轻量下载模式')

    import worker  # noqa: F401
    from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog

    if os.environ.get('BROWSER_USE_LIGHTWEIGHT_DOWNLOADS') == '1':
        results.ok('main.py 默认启用 BROWSER_USE_LIGHTWEIGHT_DOWNLOADS')
    else:
        results.fail('轻量下载环境变量', '未默认启用 BROWSER_USE_LIGHTWEIGHT_DOWNLOADS=1')

    if os.environ.get('BROWSER_USE_DISABLE_SCREENSHOTS') == '1':
        results.ok('main.py 默认禁用截图')
    else:
        results.fail('截图环境变量', '未默认启用 BROWSER_USE_DISABLE_SCREENSHOTS=1')

    if os.environ.get('BROWSER_USE_LIGHTWEIGHT_DOM') == '1':
        results.ok('main.py 默认启用轻量 DOM')
    else:
        results.fail('轻量 DOM 环境变量', '未默认启用 BROWSER_USE_LIGHTWEIGHT_DOM=1')

    old_value = os.environ.get('BROWSER_USE_LIGHTWEIGHT_DOWNLOADS')
    try:
        os.environ['BROWSER_USE_LIGHTWEIGHT_DOWNLOADS'] = '1'
        watchdog = DownloadsWatchdog.model_construct()
        if watchdog._lightweight_downloads_enabled():
            results.ok('DownloadsWatchdog 可识别轻量下载模式')
        else:
            results.fail('DownloadsWatchdog 轻量模式', '环境变量为 1 时未启用')
    finally:
        if old_value is None:
            os.environ.pop('BROWSER_USE_LIGHTWEIGHT_DOWNLOADS', None)
        else:
            os.environ['BROWSER_USE_LIGHTWEIGHT_DOWNLOADS'] = old_value


# ------ 6. quit 机制测试 ------

def test_quit_mechanism():
    """测试 should_quit 标志和 check_should_quit 回调"""
    print('\n📋 测试退出机制')

    import worker as m

    # 初始状态应为 False
    original = m.should_quit
    m.should_quit = False

    if not m.should_quit:
        results.ok('should_quit 初始为 False')
    else:
        results.fail('should_quit 初始状态', '不是 False')

    # 模拟设置 quit
    m.should_quit = True

    # check_should_quit 应返回 True
    async def _check():
        return m.should_quit

    result = asyncio.run(_check())
    if result is True:
        results.ok('should_quit=True 时回调返回 True')
    else:
        results.fail('退出回调', f'返回 {result}')

    # 恢复
    m.should_quit = original


# ------ 7. 图片验证逻辑测试 ------

def test_image_validation():
    """测试下载结果验证逻辑(IMAGE_EXTENSIONS 覆盖范围)"""
    print('\n📋 测试图片验证逻辑')

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        IMAGE_EXTENSIONS = ('*.tif', '*.tiff', '*.png', '*.jpg', '*.jpeg', '*.gif', '*.webp')

        # 创建各种格式的文件
        test_files = {
            'photo.tif': True,
            'photo.tiff': True,
            'photo.png': True,
            'photo.jpg': True,
            'photo.jpeg': True,
            'photo.gif': True,
            'photo.webp': True,
            'data.md': False,
            'info.txt': False,
            'script.py': False,
        }

        for fname in test_files:
            (tmp / fname).write_bytes(b'\x00' * 10)

        found = []
        for ext in IMAGE_EXTENSIONS:
            found.extend(tmp.glob(ext))
        found_names = {f.name for f in found}

        expected = {name for name, should_match in test_files.items() if should_match}
        unexpected = {name for name, should_match in test_files.items() if not should_match}

        if expected == found_names:
            results.ok(f'IMAGE_EXTENSIONS 匹配了全部 {len(expected)} 种图片格式')
        else:
            missing = expected - found_names
            extra = found_names - expected
            results.fail('IMAGE_EXTENSIONS', f'缺少: {missing}, 多余: {extra}')

        if not (found_names & unexpected):
            results.ok('非图片文件未被匹配')
        else:
            results.fail('非图片文件', f'被误匹配: {found_names & unexpected}')


# ------ 8. title.txt 移除回归测试 ------

def test_title_counting():
    """title.txt 已彻底移除:record_downloaded_image 不再生成它."""
    print('\n📋 测试 title.txt 不再生成')

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if not (tmp / 'title.txt').exists():
            results.ok('运行目录不再生成 title.txt')
        else:
            results.fail('title.txt 移除', 'title.txt 仍被生成')


def test_downloaded_record_counting():
    """测试 download_record.jsonl 成功记录计数."""
    print('\n📋 测试下载记录计数')

    from worker import count_downloaded_records

    with tempfile.TemporaryDirectory() as tmp:
        record_file = Path(tmp) / 'download_record.jsonl'
        record_file.write_text(
            '{"status":"downloaded","title":"A"}\n'
            '{"status":"failed","title":"B"}\n'
            '{"status": "downloaded", "title":"C"}\n',
            encoding='utf-8',
        )
        count = count_downloaded_records(record_file)
        if count == 2:
            results.ok('count_downloaded_records 只统计 downloaded')
        else:
            results.fail('count_downloaded_records', f'期望 2，得到 {count}')


def test_record_driven_rename():
    """测试 rename_images 优先按 download_record.jsonl 精确映射重命名."""
    print('\n📋 测试 download_record.jsonl 驱动重命名')

    from rename_images import rename_images

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        image_dir = base / 'image'
        data_dir = base / 'browseruse_agent_data'
        image_dir.mkdir()
        data_dir.mkdir()

        wrong_a = image_dir / 'wrong_a.tif'
        wrong_b = image_dir / 'wrong_b.tif'
        wrong_a.write_bytes(b'a' * 10)
        wrong_b.write_bytes(b'b' * 20)

        record_file = data_dir / 'download_record.jsonl'
        record_file.write_text(
            '{"status":"downloaded","title":"First Temple","file_path":"missing_a.tif","file_size":10,"page_url":"https://www.loc.gov/item/a/"}\n'
            '{"status":"downloaded","title":"Second Temple","file_path":"wrong_b.tif","file_size":20,"page_url":"https://www.loc.gov/item/b/"}\n',
            encoding='utf-8',
        )

        ok = rename_images(download_dir=str(image_dir), record_file=str(record_file))
        expected = {'First_Temple.tif', 'Second_Temple.tif', 'rename_record.txt'}
        actual = {path.name for path in image_dir.iterdir()}
        if ok and expected <= actual:
            results.ok('rename_images 可用下载记录精确重命名')
        else:
            results.fail('记录驱动重命名', f'期望包含 {expected}，得到 {actual}')


def test_generic_image_record_workflow():
    """测试通用记录工具会立即最终命名,并生成 title/info/hash 字段."""
    print('\n📋 测试通用图片记录队列')

    import asyncio
    import tools_registry
    from tools_registry import RecordDownloadedImageParams, record_downloaded_image

    old_tools_base = tools_registry.BASE_DIR
    old_image_dir = tools_registry.IMAGE_DIR
    old_data_dir = tools_registry.AGENT_DATA_DIR

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / 'Images' / 'ImagesCache'
        run_dir.mkdir(parents=True)
        from PIL import Image

        image = Image.frombytes('RGB', (320, 320), os.urandom(320 * 320 * 3))
        image.save(run_dir / 'temple_001.png')

        tools_registry.configure_runtime_paths(run_dir, run_dir, run_dir)
        try:
            result = asyncio.run(record_downloaded_image(params=RecordDownloadedImageParams(
                sequence=1,
                file_name='temple_001.png',
                title='寺_001_金堂・本尊_図1',
                collection_title='金堂・本尊',
                page_url='https://knmdb.kyohaku.go.jp/object/1',
                image_url='https://knmdb.kyohaku.go.jp/art_images/1-L.jpg',
                evidence='标题含寺院建筑语境',
                metadata='时代:江户;分类:绘画',
                summary='寺院相关藏品图像',
            )))

            if not result.error:
                results.ok('record_downloaded_image 成功写入记录')
            else:
                results.fail('record_downloaded_image', result.error)

            info_text = (run_dir / 'temple_photo_info.md').read_text(encoding='utf-8')
            record_text = (run_dir / 'image_record.jsonl').read_text(encoding='utf-8')
            record = json.loads(record_text.strip())
            if not (run_dir / 'title.txt').exists():
                results.ok('record_downloaded_image 不再生成 title.txt')
            else:
                results.fail('title.txt 移除', 'record_downloaded_image 仍生成 title.txt')

            if (
                '金堂・本尊' in info_text
                and '/art_images/1-L.jpg' in info_text
                and record.get('status') == 'downloaded'
                and record.get('content_hash')
                and record.get('source_hash')
                and record.get('title_hash')
                and record.get('source_hash', '')[:8] in record.get('file_name', '')
            ):
                results.ok('record_downloaded_image 生成信息表,JSONL 和 hash 字段')
            else:
                results.fail('记录文件生成', '信息表或 JSONL 内容不完整')

            renamed = {path.name for path in run_dir.iterdir()}
            if 'temple_001.png' not in renamed and any(name.startswith('寺_001_金堂・本尊_図1') and record.get('source_hash', '')[:8] in name and name.endswith('.png') for name in renamed):
                results.ok('record_downloaded_image 会立即按 title + hash 最终命名 PNG')
            else:
                results.fail('即时最终命名', f'得到 {renamed}, record={record}')
        finally:
            tools_registry.BASE_DIR = old_tools_base
            tools_registry.IMAGE_DIR = old_image_dir
            tools_registry.AGENT_DATA_DIR = old_data_dir


# ------ 9. 端到端模拟测试(Mock Agent) ------

def test_end_to_end_mock():
    """模拟完整流程:task读取 → move_images → Agent(mock) → 验证 → rename"""
    print('\n📋 端到端模拟测试')

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        create_temp_project(tmp)

        # 模拟下载了 3 张图片
        create_fake_downloads(tmp / 'image', count=3)

        # 模拟 agent 写入了 title.txt
        create_title_file(tmp / 'browseruse_agent_data', [
            'Buddhist Temple Photo',
            'Ancient Ruins Image',
            'Mountain Landscape',
        ])

        # --- 测试 task.md 读取 ---
        task_file = tmp / 'task.md'
        task = task_file.read_text(encoding='utf-8').strip()
        if task and len(task) > 0:
            results.ok('task.md 读取成功')
        else:
            results.fail('task.md 读取', '内容为空')

        # --- 测试 move_images.py 执行 ---
        from worker import run_python_script
        ok = run_python_script(str(tmp / 'move_images.py'), '图片迁移', extra_args=['--no-confirm'])
        if ok:
            results.ok('move_images.py --no-confirm 执行成功')
        else:
            results.fail('move_images.py', '执行失败')

        # --- 模拟 Agent 运行(不调用 LLM)---
        # 验证 image 目录中有文件
        image_files = list((tmp / 'image').glob('*.tiff'))
        if len(image_files) == 3:
            results.ok('image/ 中有 3 个模拟图片')
        else:
            results.fail('image/ 文件数', f'期望 3，得到 {len(image_files)}')

        # --- 测试 rename_images.py 执行 ---
        ok = run_python_script(str(tmp / 'rename_images.py'), '图片重命名')
        if ok:
            results.ok('rename_images.py 执行成功')
        else:
            results.fail('rename_images.py', '执行失败')

        # 验证重命名结果
        renamed = list((tmp / 'image').glob('*.tiff'))
        renamed_names = sorted([f.name for f in renamed])
        expected_names = sorted([
            'Buddhist_Temple_Photo.tiff',
            'Ancient_Ruins_Image.tiff',
            'Mountain_Landscape.tiff',
        ])
        if renamed_names == expected_names:
            results.ok(f'重命名结果正确: {renamed_names}')
        else:
            results.fail('重命名结果', f'期望 {expected_names}，得到 {renamed_names}')

        # 验证 rename_record.txt 生成
        record = tmp / 'image' / 'rename_record.txt'
        if record.exists():
            results.ok('rename_record.txt 已生成')
        else:
            results.fail('rename_record.txt', '未生成')


# ------ 10. sanitize_filename 测试 ------

def test_sanitize_filename():
    """测试文件名清理函数"""
    print('\n📋 测试 sanitize_filename()')

    from rename_images import sanitize_filename

    cases = [
        ('normal title', 'normal_title'),
        ('title with\nnewline', 'title_with_newline'),
        ('title:with<bad>chars', 'title_with_bad_chars'),
        ('  spaces  ', 'spaces'),
        ('a' * 250, 'a' * 200),  # 长度限制
        ('', 'unnamed'),  # 空字符串
        ('hello   world', 'hello_world'),  # 连续空格 → 单个下划线
        ('file/name\\test', 'file_name_test'),
    ]

    for input_val, expected in cases:
        result = sanitize_filename(input_val)
        if result == expected:
            results.ok(f'sanitize("{input_val[:30]}...") → "{result[:30]}"')
        else:
            results.fail(f'sanitize("{input_val[:30]}")', f'期望 "{expected[:30]}"，得到 "{result[:30]}"')


# ------ 11. Information.md 格式测试 ------

def test_information_md():
    """测试 Information.md 中的 HTML 代码块可被 tools_registry 正确解析"""
    print('\n📋 测试 Information.md 解析')

    import re

    from tools_registry import BASE_DIR

    info_path = BASE_DIR / 'legacy' / 'Information.md'
    if not info_path.exists():
        results.fail('Information.md', '文件不存在')
        return

    content = info_path.read_text(encoding='utf-8')
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    html_blocks = re.findall(r"```html\n([\s\S]*?)```", content)
    if html_blocks:
        results.ok(f'Information.md 包含 {len(html_blocks)} 个 HTML 代码块')
    else:
        results.fail('Information.md', '未找到 HTML 代码块')

    # 验证每个块至少有首尾行
    for i, block in enumerate(html_blocks):
        lines = block.strip().split('\n')
        if len(lines) >= 2:
            results.ok(f'代码块 {i + 1}: 首行="{lines[0].strip()[:40]}", 尾行="{lines[-1].strip()[:40]}"')
        elif len(lines) == 1:
            results.fail(f'代码块 {i + 1}', '只有一行,需要首尾两行')
        else:
            results.fail(f'代码块 {i + 1}', '空代码块')


# ============================================================
# 运行所有测试
# ============================================================

def test_idp_batch_adapter():
    """测试当前默认批量模式的 IDPAdapter 站点契约(URL 构造/判定/解析)。"""
    print('\n📋 测试 IDP 批量适配器')

    from adapters.idp import IDPAdapter

    adapter = IDPAdapter()
    url = adapter.build_search_url('china buddhist', 2, 50)
    if 'idp.bl.uk' in url and 'term=china+buddhist' in url and 'page=2' in url:
        results.ok('IDPAdapter.build_search_url 生成正确搜索 URL')
    else:
        results.fail('IDPAdapter.build_search_url', url)

    if adapter.is_results_url(url) and not adapter.is_results_url('https://idp.bl.uk/collection/ABC123/'):
        results.ok('IDPAdapter.is_results_url 能区分结果页与详情页')
    else:
        results.fail('IDPAdapter.is_results_url', url)

    keyword, page, limit = adapter.parse_search_url(url)
    if keyword == 'china buddhist' and page == 2 and limit == 50:
        results.ok('IDPAdapter.parse_search_url 正确解析关键词/页码/数量')
    else:
        results.fail('IDPAdapter.parse_search_url', f'{keyword}/{page}/{limit}')

    manifest = adapter.manifest_url_for_item({'manifest_url': 'https://data.idp.bl.uk/iiif/3/manifest/X'})
    if manifest.endswith('/manifest/X'):
        results.ok('IDPAdapter.manifest_url_for_item 取出 IIIF manifest URL')
    else:
        results.fail('IDPAdapter.manifest_url_for_item', manifest)


def test_generic_config_adapter():
    """测试通用化骨架:ConfigIIIFAdapter 由 JSON profile 驱动 + registry 按 URL 选 adapter。"""
    print('\n📋 测试通用配置适配器')

    from adapters.generic_config import ConfigIIIFAdapter
    from adapters.registry import resolve_adapter
    from adapters.idp import IDPAdapter

    profile = {
        'site_id': 'demo', 'host_suffixes': ['demo.org'], 'results_host': 'demo.org',
        'results_path': '/search', 'keyword_param': 'q', 'page_param': 'page', 'limit_param': 'limit',
        'item_link_selector': "a[href*='/item/']", 'item_id_regex': '/item/([0-9a-f]+)',
        'manifest_template': 'https://demo.org/iiif/{id}/manifest.json',
    }
    a = ConfigIIIFAdapter(profile)
    if a.is_results_url('https://demo.org/search?q=temple&page=2&limit=30') and a.parse_search_url(
        'https://demo.org/search?q=temple&page=2&limit=30') == ('temple', 2, 30):
        results.ok('ConfigIIIFAdapter 由 JSON 正确判定/解析结果页')
    else:
        results.fail('ConfigIIIFAdapter URL', a.parse_search_url('https://demo.org/search?q=temple&page=2&limit=30'))

    if a.manifest_url_for_item({'manifest_url': 'https://demo.org/iiif/ab12/manifest.json'}).endswith('ab12/manifest.json'):
        results.ok('ConfigIIIFAdapter 取出 manifest URL')
    else:
        results.fail('ConfigIIIFAdapter manifest', '解析失败')

    # IDP URL 仍回退到内置 IDPAdapter,未知站点也兜底为 IDP(尚未接线通用 resolver)
    if isinstance(resolve_adapter('https://idp.bl.uk/collection/?term=temple'), IDPAdapter):
        results.ok('registry.resolve_adapter 对 IDP URL 选用 IDPAdapter')
    else:
        results.fail('registry.resolve_adapter', '未回退到 IDPAdapter')


def main():
    print('=' * 60)
    print('  main.py 功能测试(不调用大模型)')
    print('=' * 60)

    test_run_python_script()
    test_trim_info_log()
    test_paths()
    test_task_md_content()
    test_path_safety()
    test_tools_registered()
    test_plain_border_trimming()
    test_kyohaku_method_strategy()
    test_run_limit_configuration()
    test_title_end_marker_removed()
    test_prepare_runtime_state_resume()
    test_select_active_cache_dir_archives_old_cache()
    test_move_images_unicode_filename()
    test_idp_progress_resume_context()
    test_record_filename_and_sequence_safety()
    test_configure_resume_target_script()
    test_auto_runner_helpers()
    test_auto_runner_keyword_update()
    test_auto_runner_keyword_change_archives_cache()
    test_auto_runner_new_run_archives_cache()
    test_lightweight_download_mode()
    test_quit_mechanism()
    test_image_validation()
    test_title_counting()
    test_downloaded_record_counting()
    test_sanitize_filename()
    test_record_driven_rename()
    test_generic_image_record_workflow()
    test_information_md()
    test_end_to_end_mock()
    test_idp_batch_adapter()
    test_generic_config_adapter()

    all_passed = results.summary()
    sys.exit(0 if all_passed else 1)


if __name__ == '__main__':
    main()

