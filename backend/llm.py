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
    "如果提供了参考资料（知识库条目、订单信息），请基于它们回答，"
    "并在引用时用 [来源: 标题] 标注出处。"
)


def chat_reply(
    message: str,
    history: list[dict] | None = None,
    knowledge: list[dict] | None = None,
    order: dict | None = None,
) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # 注入订单/服务范围信息（作为工具查询结果）
    if order:
        if order.get("has_order"):
            lines = [
                f"该用户的订单信息：{order['product_name']}（{order['product_tier']}），"
                f"服务状态：{'服务期内' if order['in_service'] else '服务期外'}。"
                f"包含特性：{'、'.join(order['features'])}。"
            ]
        else:
            lines = ["该用户没有查询到订单记录。"]
        messages.append({"role": "system", "content": " ".join(lines)})

    # 注入检索到的知识（作为参考资料）
    if knowledge:
        kb = "\n\n".join(
            f"[来源: {k['title']}]\n{k['content']}" for k in knowledge
        )
        messages.append(
            {
                "role": "system",
                "content": "以下是相关知识库资料，请基于它们回答并标注来源：\n\n" + kb,
            }
        )

    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})

    resp = _client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        messages=messages,
        temperature=0.7,
    )
    return resp.choices[0].message.content or ""
