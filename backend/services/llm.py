from pathlib import Path
from typing import Iterator

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


# ---------------------------------------------------------------------------
# 循环执行用
# ---------------------------------------------------------------------------

# 模型在这一轮里如果还需要更多资料，用这个标记开头提出检索请求。
# 用可解析的标记而不是自由文本，是为了让「要不要再查一次」这件事
# 由明确的协议决定，而不是靠猜模型的语气。
ACTION_PREFIX = "ACTION:"
_ACTION_PATTERN = "search_knowledge"


def build_messages(
    message: str,
    *,
    history: list[dict] | None = None,
    observations: list[str] | None = None,
    order: dict | None = None,
    knowledge: list[dict] | None = None,
    allow_action: bool = True,
) -> list[dict]:
    """组装一次请求的消息序列。

    ``allow_action`` 控制是否附加「还可以再检索」的指令：Agent 循环需要它，
    流式输出不需要 —— 流式一旦开始就无法在中途插入工具调用。
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

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

    if knowledge:
        blocks = []
        for k in knowledge:
            handle = k.get("handle")
            label = f"[{handle}] " if handle else ""
            blocks.append(f"{label}{k['title']}\n{k['content']}")
        messages.append(
            {
                "role": "system",
                "content": (
                    "以下是相关知识库资料，请基于它们回答。"
                    "每条资料前的方括号里是它的编号，引用时使用该编号：\n\n"
                    + "\n\n".join(blocks)
                ),
            }
        )

    if observations:
        messages.append(
            {
                "role": "system",
                "content": "已经查到的信息如下：\n\n" + "\n\n".join(observations),
            }
        )

    if history:
        messages.extend(history)

    if allow_action:
        messages.append(
            {
                "role": "system",
                "content": (
                    "如果你还需要继续检索才能回答，请只输出一行："
                    f"`{ACTION_PREFIX} {_ACTION_PATTERN} 关键词`。"
                    "如果信息已经足够，直接给出最终回答，不要再输出该指令。"
                ),
            }
        )

    messages.append({"role": "user", "content": message})
    return messages


def chat_step(
    message: str,
    *,
    history: list[dict] | None = None,
    observations: list[str] | None = None,
    order: dict | None = None,
    knowledge: list[dict] | None = None,
    provider: ChatProvider | None = None,
) -> str:
    """执行一轮推理：把已观察到的信息交给模型，拿到它的下一步输出。

    输出有两种形态：
    - 以 ``ACTION: search_knowledge [关键词]`` 开头：模型还想再查一次。
    - 其它：模型的最终回答。

    这里只负责组装与调用，不解析语义——解析交给调用方，
    这样模型即使不按协议输出，也能退化成「直接回答」而不报错。
    """
    messages = build_messages(
        message,
        history=history,
        observations=observations,
        order=order,
        knowledge=knowledge,
        allow_action=True,
    )
    chat = provider or _get_provider()
    return chat.chat(messages)


def chat_stream_messages(
    messages: list[dict],
    *,
    provider: ChatProvider | None = None,
) -> Iterator[str]:
    """对已组装好的消息序列做流式生成。

    单独提供这个入口，是给「自己组装消息、只需流式取结果」的调用方用，
    避免为了流式再走一遍 build_messages。
    provider 不支持流式时退化为一次性返回。
    """
    chat = provider or _get_provider()

    stream_method = getattr(chat, "chat_stream", None)
    if stream_method is None:
        text = chat.chat(messages)
        if text:
            yield text
        return
    yield from stream_method(messages)


def chat_stream(
    message: str,
    *,
    history: list[dict] | None = None,
    order: dict | None = None,
    knowledge: list[dict] | None = None,
    provider: ChatProvider | None = None,
) -> Iterator[str]:
    """流式生成最终回答，逐段产出文本增量。

    检索已在调用前完成，因此这里不附加「可再检索」指令（见 build_messages）。
    provider 不支持流式时退化为一次性返回，保证调用方无需分支处理。
    """
    messages = build_messages(
        message,
        history=history,
        order=order,
        knowledge=knowledge,
        allow_action=False,
    )
    yield from chat_stream_messages(messages, provider=provider)


def parse_action(text: str) -> dict | None:
    """从模型输出中解析检索指令；没有指令时返回 None。"""
    if not text:
        return None
    stripped = text.strip()
    if not stripped.upper().startswith(ACTION_PREFIX):
        return None
    rest = stripped[len(ACTION_PREFIX):].strip()
    if not rest:
        return None
    parts = rest.split(maxsplit=1)
    action_name = parts[0].lower()
    if action_name != _ACTION_PATTERN:
        return None
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        return None
    return {"action": action_name, "query": query}
