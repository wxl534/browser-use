"""
Test two agents running linearly (sequentially) with shared browser session.

This demonstrates:
1. Two separate Agent instances running one after another
2. Sharing the same BrowserSession to maintain state (cookies, tabs, etc.)
3. Using keep_alive=True to prevent browser from closing between agents
"""

from browser_use import Agent, Browser, ChatOpenAI
from browser_use.browser import BrowserSession
import asyncio
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import socket
import sys
import threading
import time

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent

# 全局标志:用于控制是否退出
should_quit = False


def monitor_input_windows():
    """
    Windows 平台的后台线程:监听键盘输入,如果输入 'quit' 则设置退出标志
    """
    global should_quit
    import msvcrt

    print("\n💡 提示:在运行过程中输入 'quit' 可以停止程序运行")
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
                        print("\n\n⚠️  收到退出指令,正在停止运行...")
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


def start_input_monitor():
    """
    启动输入监听线程(自动选择适合当前平台的实现)
    """
    if os.name == 'nt':  # Windows
        monitor_thread = threading.Thread(target=monitor_input_windows, daemon=True)

    monitor_thread.start()
    return monitor_thread


# 临时解决方案:绑定 hosts
# 10.64.84.182 openapi.seu.edu.cn
load_dotenv()

# === 完全禁用截图功能的环境变量配置 ===
# 增加点击事件超时时间,避免下载等待时的超时警告
os.environ['TIMEOUT_ClickElementEvent'] = '60.0'  # 从默认 15s 增加到 60s
os.environ['TIMEOUT_ScreenshotEvent'] = '60.0'  # 截图事件超时也增加
print("✅ 已配置环境变量:禁用截图功能,增加事件超时时间")


# 临时添加 host 映射,仅用在学校llm
# def add_host_mapping(host, ip):
#     """临时添加 host 映射到本地"""
#     try:
#         # 尝试解析域名,看是否已经配置
#         socket.gethostbyname(host)
#         print(f"✓ Host '{host}' 已配置")
#     except socket.gaierror:
#         print(f"⚠ 注意:需要在系统 hosts 文件中添加映射:{ip} {host}")
#         print(f"  Windows: C:\\Windows\\System32\\drivers\\etc\\hosts")
#         print(f"  以管理员身份运行记事本,添加:{ip} {host}")
#
# # 检查 host 配置
# add_host_mapping('openapi.seu.edu.cn', '10.64.84.182')

# === 导入工具函数 ===
def run_python_script(script_path: str, description: str = "脚本") -> bool:
    """
    运行 Python 脚本的辅助函数

    Args:
        script_path: 脚本的绝对路径
        description: 脚本描述

    Returns:
        是否成功执行
    """
    import subprocess
    import sys

    script = Path(script_path)

    if not script.exists():
        print(f"⚠️ 警告:{description}脚本不存在:{script}")
        return False

    try:
        # 使用当前 Python 解释器运行(避免环境问题)
        # 先以二进制模式捕获输出,避免编码错误
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            timeout=60,
            cwd=str(script.parent),  # 使用脚本所在目录作为工作目录
            env=os.environ.copy()  # 继承当前环境变量
        )

        # 手动解码输出:优先尝试 UTF-8,失败则用 GBK(Windows 中文环境)
        # errors='replace' 会用 替换无法解码的字符
        try:
            stdout = result.stdout.decode('gbk', errors='replace')
        except Exception:
            stdout = result.stdout.decode('utf-8', errors='replace')

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
    # === 3. 创建浏览器与llm实例 ===
    browser = Browser(
        # use_cloud=True,  # Use a stealth browser on Browser Use Cloud
        args=[
            f'--user-data-dir={BASE_DIR / "browser_profile"}'
        ],
        headless=False,
        enable_default_extensions=False,  # 禁用扩展以避免干扰
        keep_alive=True,  # 保持浏览器存活,让多个智能体共享会话
    )

    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('Set OPENAI_API_KEY environment variable; do NOT hardcode API keys in source code')
    base_url = os.getenv('OPENAI_BASE_URL', 'https://openapi.seu.edu.cn/v1')

    llm = ChatOpenAI(
        model='qwen3.5-397b-a17b',
        api_key=api_key,
        base_url=base_url,
        temperature=0.0
    )
    # llm = ChatBrowserUse(),  # 官方llm,需写在agent中

    # === 4. 启动输入监听线程 ===
    input_thread = start_input_monitor()

    try:
        # === 5. 创建并运行第一个 Agent ===
        print("\n" + "="*60)
        print("🤖 开始执行第一个智能体任务...")
        print("="*60)
        
        agent1 = Agent(
            task="访问 https://www.baidu.com 并搜索 'Browser Use',然后打开第一个搜索结果",  # 第一个任务
            llm=llm,
            browser=browser,
            use_vision=False,  # 完全关闭视觉识别和截图
            max_failures=3,  # 降低失败重试次数
            max_actions_per_step=3,  # 允许每步执行更多动作提高效率
            step_timeout=180,  # 增加单步超时时间
            llm_timeout=120,  # 增加 LLM 超时时间
            file_system_path=str(BASE_DIR),
        )

        print("\n🚀 第一个智能体开始执行任务...")
        history1 = await agent1.run(max_steps=1000)
        
        # 检查是否是因为 quit 命令而停止
        if should_quit:
            print("\n🛑 程序已被用户手动停止(第一个智能体执行期间)")
            print("\n=== 第一个智能体任务统计(截至停止时)===")
            print(f"总步数：{history1.number_of_steps()}")
            print(f"总耗时：{history1.total_duration_seconds():.2f} 秒")
            print(f"访问 URL 数：{len(history1.urls())}")
            return None
        
        print("\n✅ 第一个智能体任务完成!")
        print(f"   - 总步数：{history1.number_of_steps()}")
        print(f"   - 总耗时：{history1.total_duration_seconds():.2f} 秒")
        print(f"   - 访问 URL 数：{len(history1.urls())}")
        
        # === 6. 创建并运行第二个 Agent(共享同一个浏览器会话)===
        print("\n" + "="*60)
        print("🤖 开始执行第二个智能体任务...")
        print("="*60)
        
        agent2 = Agent(
            task="在当前页面提取标题和主要内容,保存到 result.md 文件中",  # 第二个任务
            llm=llm,
            browser=browser,  # 使用同一个浏览器对象,保持会话状态
            use_vision=False,  # 完全关闭视觉识别和截图
            max_failures=3,
            max_actions_per_step=3,
            step_timeout=180,
            llm_timeout=120,
            file_system_path=str(BASE_DIR),
        )

        print("\n🚀 第二个智能体开始执行任务...")
        history2 = await agent2.run(max_steps=1000)
        
        # 检查是否是因为 quit 命令而停止
        if should_quit:
            print("\n🛑 程序已被用户手动停止(第二个智能体执行期间)")
            print("\n=== 第二个智能体任务统计(截至停止时)===")
            print(f"总步数：{history2.number_of_steps()}")
            print(f"总耗时：{history2.total_duration_seconds():.2f} 秒")
            print(f"访问 URL 数：{len(history2.urls())}")
            return None
        
        print("\n✅ 第二个智能体任务完成!")
        print(f"   - 总步数：{history2.number_of_steps()}")
        print(f"   - 总耗时：{history2.total_duration_seconds():.2f} 秒")
        print(f"   - 访问 URL 数：{len(history2.urls())}")
        
        # === 7. 汇总两个智能体的结果 ===
        print("\n" + "="*60)
        print("📊 所有任务执行完成!汇总统计:")
        print("="*60)
        print(f"第一个智能体：{history1.number_of_steps()} 步，耗时 {history1.total_duration_seconds():.2f} 秒")
        print(f"第二个智能体：{history2.number_of_steps()} 步，耗时 {history2.total_duration_seconds():.2f} 秒")
        print(f"总计：{history1.number_of_steps() + history2.number_of_steps()} 步，总耗时 {history1.total_duration_seconds() + history2.total_duration_seconds():.2f} 秒")
        
        # === 8. 手动关闭浏览器(因为 keep_alive=True)===
        print("\n🔒 正在关闭浏览器...")
        await browser.close()
        print("✅ 浏览器已关闭")
        
        return history1, history2
        
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断执行 (Ctrl+C)")
        # 中断时也要关闭浏览器
        print("\n🔒 正在关闭浏览器...")
        try:
            await browser.close()
            print("✅ 浏览器已关闭")
        except Exception as e:
            print(f"⚠️ 关闭浏览器时出错: {e}")
        return None


if __name__ == "__main__":
    asyncio.run(main())