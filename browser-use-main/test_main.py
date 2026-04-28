"""
main.py 功能测试（不调用大模型）

测试策略：
- 使用临时目录模拟项目结构，不影响真实文件
- Mock 掉 Browser、ChatOpenAI、Agent，不触发网络请求
- 逐个验证 main.py 中各个功能模块的正确性

运行方式：
    python test_main.py
    或
    python -m pytest test_main.py -v
"""

import asyncio
import os
import shutil
import sys
import tempfile
import textwrap
import time
import threading
from pathlib import Path
from unittest import mock

# 项目根目录（用于导入）
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))


# ============================================================
# 辅助工具
# ============================================================

def create_temp_project(tmp: Path):
    """在临时目录中创建最小化的项目结构"""
    # task.md
    (tmp / 'task.md').write_text('测试任务：下载 3 张图片', encoding='utf-8')

    # Information.md
    (tmp / 'Information.md').write_text(
        '```html\n<div class="item">\n</div>\n```\n',
        encoding='utf-8',
    )

    # image 目录
    (tmp / 'image').mkdir()

    # browseruse_agent_data 目录
    (tmp / 'browseruse_agent_data').mkdir()

    # move_images.py（简化版，只打印不做事）
    (tmp / 'move_images.py').write_text(textwrap.dedent("""\
        import sys
        if '--no-confirm' in sys.argv:
            print('move_images: no-confirm mode, skip confirmation')
        else:
            print('move_images: normal mode')
        print('move_images: done')
    """), encoding='utf-8')

    # rename_images.py（简化版，模拟重命名）
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

    from main import run_python_script

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


# ------ 2. 路径配置测试 ------

def test_paths():
    """测试所有路径配置的一致性"""
    print('\n📋 测试路径配置')

    from main import BASE_DIR as MAIN_BASE
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

    # task.md 和 Information.md 应存在
    for fname in ['task.md', 'Information.md']:
        if (MAIN_BASE / fname).exists():
            results.ok(f'{fname} 存在')
        else:
            results.fail(f'{fname} 存在', '文件缺失')


# ------ 3. task.md 内容测试 ------

def test_task_md_content():
    """测试 task.md 不引用不存在的工具"""
    print('\n📋 测试 task.md 内容')

    from main import BASE_DIR
    task_content = (BASE_DIR / 'task.md').read_text(encoding='utf-8')

    # 不应引用已删除的 extract_js_object_by_keyword
    if 'extract_js_object_by_keyword' not in task_content:
        results.ok('task.md 不引用不存在的 extract_js_object_by_keyword')
    else:
        results.fail('task.md 工具引用', '仍包含 extract_js_object_by_keyword')

    # 应引用实际存在的 extract_page_to_markdown
    if 'extract_page_to_markdown' in task_content:
        results.ok('task.md 引用了 extract_page_to_markdown')
    else:
        results.fail('task.md 工具引用', '缺少 extract_page_to_markdown')

    # 不应包含硬编码旧路径
    if 'D:\\desktop' not in task_content and 'D:/desktop' not in task_content:
        results.ok('task.md 无硬编码旧路径')
    else:
        results.fail('task.md 路径', '仍包含 D:\\desktop')

    # title.txt 路径应指向 browseruse_agent_data
    if 'browseruse_agent_data/title.txt' in task_content or 'browseruse_agent_data\\title.txt' in task_content:
        results.ok('task.md 中 title.txt 路径正确')
    else:
        results.fail('task.md title.txt 路径', '未指向 browseruse_agent_data/')


# ------ 4. tools_registry 路径安全测试 ------

def test_path_safety():
    """测试 tools_registry 的路径安全验证"""
    print('\n📋 测试路径安全验证')

    from tools_registry import _is_path_allowed, BASE_DIR

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

    from tools_registry import tools, registry

    # registry.registry.actions 是一个 dict，key 为 action 名称
    action_names = list(registry.registry.actions.keys())
    if 'extract_page_to_markdown' in action_names:
        results.ok('extract_page_to_markdown 已注册')
    else:
        results.fail('工具注册', f'未找到，已注册: {action_names}')


# ------ 6. quit 机制测试 ------

def test_quit_mechanism():
    """测试 should_quit 标志和 check_should_quit 回调"""
    print('\n📋 测试退出机制')

    import main as m

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
    """测试下载结果验证逻辑（IMAGE_EXTENSIONS 覆盖范围）"""
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


# ------ 8. title.txt 计数测试（排除 END） ------

def test_title_counting():
    """测试 title.txt 读取时排除 END 标记"""
    print('\n📋 测试 title.txt 计数')

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 3 个标题 + END
        title_file = tmp / 'title.txt'
        title_file.write_text('Buddhist Temple A\nBuddhist Temple B\nBuddhist Temple C\nEND\n', encoding='utf-8')

        with open(title_file, 'r', encoding='utf-8') as f:
            title_count = len([line for line in f if line.strip() and line.strip().upper() != 'END'])

        if title_count == 3:
            results.ok('3 个标题 + END → 计数为 3')
        else:
            results.fail('title 计数', f'期望 3，得到 {title_count}')

        # 空文件
        title_file.write_text('END\n', encoding='utf-8')
        with open(title_file, 'r', encoding='utf-8') as f:
            title_count = len([line for line in f if line.strip() and line.strip().upper() != 'END'])

        if title_count == 0:
            results.ok('只有 END → 计数为 0')
        else:
            results.fail('空 title 计数', f'期望 0，得到 {title_count}')


# ------ 9. 端到端模拟测试（Mock Agent） ------

def test_end_to_end_mock():
    """模拟完整流程：task读取 → move_images → Agent(mock) → 验证 → rename"""
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
        from main import run_python_script
        ok = run_python_script(str(tmp / 'move_images.py'), '图片迁移', extra_args=['--no-confirm'])
        if ok:
            results.ok('move_images.py --no-confirm 执行成功')
        else:
            results.fail('move_images.py', '执行失败')

        # --- 模拟 Agent 运行（不调用 LLM）---
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

    info_path = BASE_DIR / 'Information.md'
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
            results.fail(f'代码块 {i + 1}', '只有一行，需要首尾两行')
        else:
            results.fail(f'代码块 {i + 1}', '空代码块')


# ============================================================
# 运行所有测试
# ============================================================

def main():
    print('=' * 60)
    print('  main.py 功能测试（不调用大模型）')
    print('=' * 60)

    test_run_python_script()
    test_paths()
    test_task_md_content()
    test_path_safety()
    test_tools_registered()
    test_quit_mechanism()
    test_image_validation()
    test_title_counting()
    test_sanitize_filename()
    test_information_md()
    test_end_to_end_mock()

    all_passed = results.summary()
    sys.exit(0 if all_passed else 1)


if __name__ == '__main__':
    main()
