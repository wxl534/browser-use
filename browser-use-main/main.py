import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from browser_use import Agent, Browser, ChatOpenAI
from browser_use.llm.browser_use.chat import ChatBrowserUse

# 从独立的工具注册模块导入
from tools_registry import tools

# 项目根目录（基于脚本位置，不再硬编码）
BASE_DIR = Path(__file__).resolve().parent

# 全局标志：用于控制是否退出
should_quit = False

def monitor_input_windows():
    """
    Windows 平台的后台线程：监听键盘输入，如果输入 'quit' 则设置退出标志
    """
    global should_quit
    import msvcrt
    
    print("\n💡 提示：在运行过程中输入 'quit' 可以停止程序运行")
    print("=" * 60 + "\n")
    
    input_buffer = []
    
    try:
        while not should_quit:
            # 非阻塞检查键盘输入
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                
                # 处理回车键
                if char == '\r' or char == '\n':
                    command = ''.join(input_buffer).strip().lower()
                    input_buffer = []
                    
                    if command == 'quit':
                        print("\n\n⚠️  收到退出指令，正在停止运行...")
                        should_quit = True
                        break
                    elif command:  # 忽略空输入
                        print(f"\n⚠️  未知命令: {command}，请输入 'quit' 停止运行")
                elif char == '\b' or char == '\x08':  # 退格键
                    if input_buffer:
                        input_buffer.pop()
                        print('\b \b', end='', flush=True)  # 删除屏幕上的字符
                elif char == '\x03':  # Ctrl+C
                    should_quit = True
                    break
                else:
                    input_buffer.append(char)
                    print(char, end='', flush=True)  # 显示输入的字符
            
            # 短暂休眠避免占用过多 CPU
            time.sleep(0.01)
    except KeyboardInterrupt:
        should_quit = True
    except Exception as e:
        print(f"\n⚠️  输入监听异常: {e}")
        should_quit = True

def monitor_input_default():
    """
    非 Windows 平台的后台线程：监听终端输入
    """
    global should_quit
    
    print("\n💡 提示：在运行过程中输入 'quit' 可以停止程序运行")
    print("=" * 60 + "\n")
    
    try:
        while not should_quit:
            try:
                line = sys.stdin.readline()
                if line:
                    command = line.strip().lower()
                    if command == 'quit':
                        print("\n\n⚠️  收到退出指令，正在停止运行...")
                        should_quit = True
                        break
            except Exception:
                pass
            time.sleep(0.1)
    except KeyboardInterrupt:
        should_quit = True

def start_input_monitor():
    """
    启动输入监听线程（自动选择适合当前平台的实现）
    """
    if os.name == 'nt':  # Windows
        monitor_thread = threading.Thread(target=monitor_input_windows, daemon=True)
    else:  # Linux/Mac
        monitor_thread = threading.Thread(target=monitor_input_default, daemon=True)
    
    monitor_thread.start()
    return monitor_thread

# 临时解决方案：绑定 hosts
# 10.64.84.182 openapi.seu.edu.cn
load_dotenv()

# === 完全禁用截图功能的环境变量配置 ===
# 增加点击事件超时时间，避免下载等待时的超时警告
os.environ['TIMEOUT_ClickElementEvent'] = '60.0'  # 从默认 15s 增加到 60s
os.environ['TIMEOUT_ScreenshotEvent'] = '60.0'    # 截图事件超时也增加
print("✅ 已配置环境变量：禁用截图功能，增加事件超时时间")

# 临时添加 host 映射,仅用在学校llm
# def add_host_mapping(host, ip):
#     """临时添加 host 映射到本地"""
#     try:
#         # 尝试解析域名，看是否已经配置
#         socket.gethostbyname(host)
#         print(f"✓ Host '{host}' 已配置")
#     except socket.gaierror:
#         print(f"⚠ 注意：需要在系统 hosts 文件中添加映射：{ip} {host}")
#         print(f"  Windows: C:\\Windows\\System32\\drivers\\etc\\hosts")
#         print(f"  以管理员身份运行记事本，添加：{ip} {host}")
#
# # 检查 host 配置
# add_host_mapping('openapi.seu.edu.cn', '10.64.84.182')

# === 导入工具函数 ===
def run_python_script(script_path: str, description: str = "脚本", extra_args: list[str] | None = None) -> bool:
    """
    运行 Python 脚本的辅助函数
    
    Args:
        script_path: 脚本的绝对路径
        description: 脚本描述
        extra_args: 额外的命令行参数
        
    Returns:
        是否成功执行
    """
    script = Path(script_path)
    
    if not script.exists():
        print(f"⚠️ 警告:{description}脚本不存在:{script}")
        return False
    
    try:
        cmd = [sys.executable, str(script)]
        if extra_args:
            cmd.extend(extra_args)
        # 使用当前 Python 解释器运行(避免环境问题)
        # 先以二进制模式捕获输出,避免编码错误
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            cwd=str(script.parent),  # 使用脚本所在目录作为工作目录
            env=os.environ.copy()  # 继承当前环境变量
        )
        
        # 手动解码输出:优先尝试 UTF-8,失败则用 GBK(Windows 中文环境)
        try:
            stdout = result.stdout.decode('utf-8')
        except UnicodeDecodeError:
            try:
                stdout = result.stdout.decode('gbk', errors='replace')
            except Exception:
                stdout = result.stdout.decode('utf-8', errors='replace')
        
        try:
            stderr = result.stderr.decode('utf-8')
        except UnicodeDecodeError:
            try:
                stderr = result.stderr.decode('gbk', errors='replace')
            except Exception:
                stderr = result.stderr.decode('utf-8', errors='replace')
        
        # 打印输出
        if stdout:
            print(f"\n📝 {description}输出:")
            print(stdout)
        
        if stderr:
            print(f"\n⚠️ {description}警告/错误:")
            print(stderr)
        
        if result.returncode == 0:
            print(f"\n✅ {description}完成!")
            return True
        else:
            print(f"\n❌ {description}失败,返回码:{result.returncode}")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"\n❌ {description}超时(超过 60 秒)")
        return False
    except Exception as e:
        print(f"\n❌ 执行{description}时出错:{e}")
        return False


async def main():
    # === 1. 从 task.md 文件读取任务描述 ===
    task_file = BASE_DIR / 'task.md'

    if not task_file.exists():
        print(f"❌ Task 文件不存在：{task_file}")
        raise FileNotFoundError(f"Task file not found: {task_file}")

    print(f"📄 从文件读取 task: {task_file}")
    with open(task_file, 'r', encoding='utf-8') as f:
        task = f.read().strip()
    print(f"✅ 成功读取 task，长度：{len(task)} 字符")

    # === 2. 运行 move_images.py 清空并转移图片 ===
    print("\n" + "=" * 60)
    print("📦 步骤 1: 执行图片迁移脚本...")
    print("=" * 60)
    
    move_script = BASE_DIR / 'move_images.py'
    # 传入 --no-confirm 避免 subprocess 中的 input() 阻塞
    run_python_script(str(move_script), "图片迁移", extra_args=['--no-confirm'])
    
    # === 2.5 清空 Information.md（功能已实现，暂时注释） ===
    # 取消下面两行注释即可启用 Information.md 自动清理：
    # from move_images import clear_information_md
    # clear_information_md(interactive=False)
    
    # 等待一下确保文件系统更新完成
    await asyncio.sleep(1)
    
    # === 3. 创建浏览器与llm实例 ===
    image_dir = BASE_DIR / 'image'
    image_dir.mkdir(parents=True, exist_ok=True)

    browser = Browser(
        args=[
            f'--user-data-dir={BASE_DIR / "browser_profile"}'
        ],
        headless=False,
        enable_default_extensions=False,
        downloads_path=str(image_dir),  # 下载文件保存到 image 目录
    )

    api_key = '9c2fcf1e-afc3-4dc4-8b7e-636cdac31519'
    base_url = 'https://openapi.seu.edu.cn/v1'

    llm = ChatOpenAI(
        model='qwen3.5-397b-a17b',
        api_key=api_key,
        base_url=base_url,
        temperature=0.0
    )
    # llm = ChatBrowserUse()  # 官方 LLM，需付费订阅

    # quit 回调：agent 每步执行前会调用此函数，返回 True 则停止
    async def check_should_quit() -> bool:
        return should_quit

    # === 4. 创建 Agent（完全禁用截图，使用 JS 提取） ===
    agent = Agent(
        task=task,  # 使用从.md 文件读取的 task
        llm=llm,
        browser=browser,
        tools=tools,
        use_vision=False,
        max_failures=3,
        max_actions_per_step=3,
        step_timeout=180,
        llm_timeout=120,
        register_should_stop_callback=check_should_quit,
        file_system_path=str(BASE_DIR),
        available_file_paths=[
            str(BASE_DIR / 'image'),
            str(BASE_DIR / 'browseruse_agent_data'),
            str(BASE_DIR / 'Information.md'),
            str(BASE_DIR / 'source.html'),
            str(BASE_DIR / 'browseruse_agent_data' / 'title.txt'),
            str(BASE_DIR / 'image' / 'rename_record.txt'),
        ],
    )
    
    # === 5. 运行 agent ===
    print("\n🚀 开始执行任务...")
    
    # 启动输入监听线程
    input_thread = start_input_monitor()
    
    try:
        history = await agent.run(max_steps=1000)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断执行 (Ctrl+C)")
        return None
    
    # 检查是否是因为 quit 命令而停止
    if should_quit:
        print("\n🛑 程序已被用户手动停止")
        print("\n=== 任务统计（截至停止时）===")
        print(f"总步数：{history.number_of_steps()}")
        print(f"总耗时：{history.total_duration_seconds():.2f} 秒")
        print(f"访问 URL 数：{len(history.urls())}")
        return history

    # === 添加验证逻辑 ===
    print("\n=== 下载结果验证 ===")

    # 检查下载目录
    download_dirs = [image_dir]

    IMAGE_EXTENSIONS = ('*.tif', '*.tiff', '*.png', '*.jpg', '*.jpeg', '*.gif', '*.webp')
    all_image_files = []
    for img_dir in download_dirs:
        if img_dir.exists():
            # 查找所有常见图片格式
            found = []
            for ext in IMAGE_EXTENSIONS:
                found.extend(img_dir.glob(ext))
            all_image_files.extend(found)
            print(f"✓ 目录 {img_dir} 中找到 {len(found)} 个图片文件")
        else:
            print(f"ℹ️ 目录不存在：{img_dir}")
    
    if all_image_files:
        print(f"\n总共找到 {len(all_image_files)} 个下载的文件:")
        for img_file in sorted(all_image_files):
            file_size = img_file.stat().st_size
            print(f"  - {img_file.name}: {file_size:,} 字节")
            if file_size == 0:
                print(f"  ⚠️ 警告：{img_file.name} 文件大小为 0")
    else:
        print("❌ 未找到任何下载的文件")
        
        # 检查 title.txt 是否存在
        title_file = BASE_DIR / 'browseruse_agent_data' / 'title.txt'
        if title_file.exists():
            with open(title_file, 'r', encoding='utf-8') as f:
                title_count = len([line for line in f if line.strip() and line.strip().upper() != 'END'])
            print(f"✓ title.txt 存在，包含 {title_count} 个标题")
        else:
            print(f"⚠️ title.txt 不存在")

    # 检查历史中的错误
    errors = history.errors()
    if any(errors):
        error_count = sum(1 for e in errors if e is not None)
        print(f"\n⚠️ 执行过程中出现 {error_count} 个错误")


    # 输出最终统计
    print(f"\n=== 任务统计 ===")
    print(f"总步数：{history.number_of_steps()}")
    print(f"总耗时：{history.total_duration_seconds():.2f} 秒")
    print(f"访问 URL 数：{len(history.urls())}")
    
    # === 6. 自动执行重命名脚本 ===
    print("\n" + "=" * 60)
    print("🔄 步骤 3: 执行图片重命名脚本...")
    print("=" * 60)
    
    rename_script = BASE_DIR / 'rename_images.py'
    
    # 使用改进的脚本执行函数
    success = run_python_script(str(rename_script), "图片重命名")
    
    if success:
        # 显示重命名后的文件
        renamed_files = []
        for ext in IMAGE_EXTENSIONS:
            renamed_files.extend(image_dir.glob(ext))
        if renamed_files:
            print(f"\n📁 重命名后的文件列表 (共 {len(renamed_files)} 个):")
            for f in sorted(renamed_files):
                if not f.name.startswith('image_'):
                    print(f"  - {f.name}")
        
        # 显示重命名记录文件
        record_file = BASE_DIR / 'image' / 'rename_record.txt'
        if record_file.exists():
            print(f"\n📄 重命名记录已保存到：{record_file}")
    else:
        print("💡 请检查错误信息并手动执行：python rename_images.py")

    return history

if __name__ == "__main__":
    asyncio.run(main())