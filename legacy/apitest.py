"""
OpenAI 兼容接口问答测试脚本.

用法:
1. 交互问答:
   uv run python apitest.py

2. 单次提问:
   uv run python apitest.py "请用一句话介绍京都国立博物馆"

3. 带图片提问:
   uv run python apitest.py --image "D:\\desktop\\image.png" "请描述这张图片"

需要在 .env 或环境变量中配置:
- OPENAI_API_KEY
- OPENAI_BASE_URL,可选,默认 https://openapi.seu.edu.cn/v1
- OPENAI_MODEL,可选,默认 qwen3.5-397b-a17b
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_BASE_URL = "https://openapi.seu.edu.cn/v1"
DEFAULT_MODEL = "qwen3.5-397b-a17b"
EXIT_COMMANDS = {"q", "quit", "exit", "退出"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def build_client() -> tuple[OpenAI, str]:
    """Create an OpenAI-compatible client from environment variables."""
    load_dotenv()

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未设置 OPENAI_API_KEY,请先在 .env 或环境变量中配置.")

    base_url = os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL).strip()
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL).strip()

    return OpenAI(api_key=api_key, base_url=base_url), model


def encode_image(image_path: str) -> dict[str, Any]:
    """Convert a local image path to an OpenAI-compatible image_url content block."""
    path = Path(image_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"图片文件不存在：{path}")
    if path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise ValueError(f"不支持的图片格式：{path.suffix}，请使用 jpg、png、webp、gif、bmp 或 tiff。")

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    image_data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{mime_type};base64,{image_data}",
        },
    }


def build_user_content(question: str, image_paths: list[str] | None = None) -> str | list[dict[str, Any]]:
    """Build either text-only or multimodal message content."""
    if not image_paths:
        return question

    content: list[dict[str, Any]] = [{"type": "text", "text": question}]
    content.extend(encode_image(image_path) for image_path in image_paths)
    return content


def ask_question(
    client: OpenAI,
    model: str,
    question: str,
    history: list[dict[str, Any]],
    image_paths: list[str] | None = None,
) -> str:
    """Send one question and return the model answer."""
    user_content = build_user_content(question, image_paths)
    messages = [
        {"role": "system", "content": "你是一个简洁,准确的中文问答助手."},
        *history,
        {"role": "user", "content": user_content},
    ]

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
    )

    answer = response.choices[0].message.content or ""
    image_note = ""
    if image_paths:
        image_names = ", ".join(Path(path).name for path in image_paths)
        image_note = f"\n[本轮附带图片：{image_names}]"
    history.extend(
        [
            {"role": "user", "content": f"{question}{image_note}"},
            {"role": "assistant", "content": answer},
        ]
    )
    return answer


def run_once(client: OpenAI, model: str, question: str, image_paths: list[str]) -> None:
    """Ask one question from command-line arguments."""
    answer = ask_question(client, model, question, history=[], image_paths=image_paths)
    print(answer)


def run_interactive(client: OpenAI, model: str) -> None:
    """Start a terminal Q&A loop."""
    history: list[dict[str, Any]] = []
    print(f"问答模式已启动，模型：{model}")
    print("输入问题后回车;输入 q,quit,exit 或 退出 结束.")
    print("如需发送图片,输入 /image,然后按提示输入图片路径和问题.多个图片路径用 | 分隔.")

    while True:
        question = input("\n你:").strip()
        if not question:
            continue
        if question.lower() in EXIT_COMMANDS:
            print("已退出.")
            return

        image_paths: list[str] = []
        if question.lower().startswith("/image"):
            inline_path = question[6:].strip()
            paths_text = inline_path or input("图片路径:").strip()
            image_paths = [path.strip().strip('"') for path in paths_text.split("|") if path.strip()]
            question = input("问题:").strip()
            if not question:
                print("问题不能为空.")
                continue

        try:
            answer = ask_question(client, model, question, history, image_paths=image_paths)
        except Exception as exc:
            print(f"请求失败：{exc}")
            continue

        print(f"\n助手：{answer}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenAI 兼容接口问答测试脚本,支持文本和图片输入.")
    parser.add_argument("question", nargs="*", help="单次提问内容;不提供时进入交互模式.")
    parser.add_argument("-i", "--image", action="append", default=[], help="图片路径,可重复传入多张图片.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client, model = build_client()
    question = " ".join(args.question).strip()

    if question:
        run_once(client, model, question, args.image)
    else:
        run_interactive(client, model)


if __name__ == "__main__":
    main()
