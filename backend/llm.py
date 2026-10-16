import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# 加载 backend/.env
load_dotenv(Path(__file__).resolve().parent / ".env")

_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)

SYSTEM_PROMPT = (
    "你是一家虚拟软件公司的智能客服助手。"
    "负责帮助用户解决产品使用问题，并判断是否需要转人工或提交工单。"
    "回答要专业、简洁、友好。"
)


def chat_reply(message: str, history: list[dict] | None = None) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})

    resp = _client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        messages=messages,
        temperature=0.7,
    )
    return resp.choices[0].message.content or ""
