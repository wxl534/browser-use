## Qwen [example](https://github.com/browser-use/browser-use/blob/main/examples/models/qwen.py)

#Currently, only `qwen-vl-max` is recommended for Browser Use. Other Qwen models, including `qwen-max`, have issues with the action schema format.
#Smaller Qwen models may return incorrect action schema formats (e.g., `actions: [{"navigate": "google.com"}]` instead of `[{"navigate": {"url": "google.com"}}]`). If you want to use other models, add concrete examples of the correct action format to your prompt.
from browser_use import Agent, Browser, ChatBrowserUse
from browser_use import Agent, ChatOpenAI
from dotenv import load_dotenv
import os
import socket

from pathlib import Path

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# 临时添加 host 映射
def add_host_mapping(host, ip):
    """临时添加 host 映射到本地"""
    try:
        # 尝试解析域名，看是否已经配置
        socket.gethostbyname(host)
        print(f"✓ Host '{host}' 已配置")
    except socket.gaierror:
        print(f"⚠ 注意：需要在系统 hosts 文件中添加映射：{ip} {host}")
        print(f"  Windows: C:\\Windows\\System32\\drivers\\etc\\hosts")
        print(f"  以管理员身份运行记事本，添加：{ip} {host}")

# 检查 host 配置
add_host_mapping('openapi.seu.edu.cn', '10.64.84.182')

# API 配置
api_key = '9c2fcf1e-afc3-4dc4-8b7e-636cdac31519'
base_url = 'https://openapi.seu.edu.cn/v1'

# 使用 qwen3.5-397b-a17b 模型
llm = ChatOpenAI(
    model='qwen3.5-397b-a17b',
    api_key=api_key,
    base_url=base_url,
    temperature=0.0
)

browser = Browser(
        # use_cloud=True,  # Use a stealth browser on Browser Use Cloud
        args=[
            f'--user-data-dir={BASE_DIR / "browser_profile"}'
        ],
        headless=False,
        enable_default_extensions=False,  # 禁用扩展以避免干扰
    )

agent = Agent(
    task = "访问 https://www.bing.com 并搜索'Browser Use'，返回第一个结果的标题",
    llm=llm,
    browser=browser,
    use_vision=True
)

async def main():
    try:
        print("开始测试 qwen3.5-397b-a17b 模型...")
        history = await agent.run(max_steps=10)
        print("\n测试完成!")
        print(f"访问的 URL: {history.urls()}")
        print(f"执行的操作：{history.action_names()}")
        if history.final_result():
            print(f"最终结果：{history.final_result()}")
    except Exception as e:
        print(f"\n❌ 测试失败：{str(e)}")
        print("\n可能的原因:")
        print("1. 检查 host 映射是否正确配置")
        print("2. 检查 API key 是否有效")
        print("3. 检查网络连接是否正常")
        print("4. 确认模型名称是否正确")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

# Required environment variables:

#```bash .env
# ALIBABA_CLOUD=
#```

# 或者直接在代码中使用上面的 api_key
