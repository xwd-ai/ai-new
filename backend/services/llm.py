from pathlib import Path

from dotenv import load_dotenv

from backend.providers import ChatProvider, build_chat_provider

# 加载 backend/.env（本文件位于 backend/services/ 下，需向上一层）
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Chat 能力由适配层提供，此处只做业务上下文的组装；
# provider 由调用方注入，避免在导入期固化成全局单例。
_default_provider: ChatProvider | None = None


def _get_provider() -> ChatProvider:
    """惰性装配默认 Chat 实现，首次调用时才创建客户端。"""
    global _default_provider
    if _default_provider is None:
        _default_provider = build_chat_provider()
    return _default_provider


SYSTEM_PROMPT = (
    "你是一家虚拟软件公司的智能客服助手。"
    "负责帮助用户解决产品使用问题，并判断是否需要转人工或提交工单。"
    "回答要专业、简洁、友好。"
    "如果提供了参考资料（知识库条目、订单信息），请基于它们回答。"
    "引用资料时，在对应句子后面写 <ref id=\"cN\"/> 标注出处，"
    "其中 cN 是资料编号；只引用确实支持该句的编号，"
    "没有编号或编号不存在的资料不要引用。"
)


def chat_reply(
    message: str,
    history: list[dict] | None = None,
    knowledge: list[dict] | None = None,
    order: dict | None = None,
    provider: ChatProvider | None = None,
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
    # knowledge 中的条目若带 handle，则以编号形式呈现，模型据此引用
    if knowledge:
        blocks = []
        for k in knowledge:
            handle = k.get("handle")
            label = f"[{handle}] " if handle else ""
            blocks.append(f"{label}{k['title']}\n{k['content']}")
        kb = "\n\n".join(blocks)
        messages.append(
            {
                "role": "system",
                "content": (
                    "以下是相关知识库资料，请基于它们回答。"
                    "每条资料前的方括号里是它的编号，引用时使用该编号：\n\n" + kb
                ),
            }
        )

    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})

    chat = provider or _get_provider()
    return chat.chat(messages)
