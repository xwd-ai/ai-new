import time
import uuid

from backend.database import SessionLocal
from backend.services import citations, knowledge, llm, orders
from backend.models import Conversation, Message, TriedStep
from backend.providers import build_chat_provider

# 对话链路：会话与消息的存取、已尝试步骤的记录，以及「查订单 + 检索手册 + 生成回答」的编排。
# 路由定义在 backend.api.chat，此处仅承载存储与业务编排逻辑。

# 对话链路的 Chat 能力，由适配层装配；可在测试时替换为假实现
_chat_provider = build_chat_provider()


def current_user(authorization: str):
    from backend.services import auth

    token = authorization.replace("Bearer ", "").strip()
    session = auth.get_session(token)
    if not session:
        return None
    return auth.find_by_username(session["username"])


def append_message(conversation_id: str, role: str, content: str) -> None:
    """向数据库追加一条消息，并刷新会话更新时间。"""
    db = SessionLocal()
    try:
        db.add(
            Message(
                id=uuid.uuid4().hex,
                conversation_id=conversation_id,
                role=role,
                content=content,
                at=time.time(),
            )
        )
        conv = (
            db.query(Conversation)
            .filter(Conversation.id == conversation_id)
            .first()
        )
        if conv:
            conv.updated_at = time.time()
        db.commit()
    finally:
        db.close()


def append_tried_step(user_id: str, step: str) -> None:
    """记录一次「已尝试步骤」，供后续建单沉淀。"""
    db = SessionLocal()
    try:
        db.add(
            TriedStep(
                id=uuid.uuid4().hex,
                user_id=user_id,
                step=step,
                at=time.time(),
            )
        )
        db.commit()
    finally:
        db.close()


def get_tried_steps(user_id: str) -> str:
    db = SessionLocal()
    try:
        steps = (
            db.query(TriedStep)
            .filter(TriedStep.user_id == user_id)
            .order_by(TriedStep.at.asc())
            .all()
        )
        return " · ".join(s.step for s in steps) if steps else ""
    finally:
        db.close()


def get_or_create_conversation(user_id: str) -> str:
    """取该用户最近一次会话；没有则新建一个。"""
    db = SessionLocal()
    try:
        conv = (
            db.query(Conversation)
            .filter(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if conv:
            return conv.id
        now = time.time()
        conv = Conversation(
            id=uuid.uuid4().hex,
            user_id=user_id,
            title="",
            created_at=now,
            updated_at=now,
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        return conv.id
    finally:
        db.close()


def history(conversation_id: str, limit: int = 8) -> list[dict]:
    """拉取最近 N 条消息作为上下文。"""
    db = SessionLocal()
    try:
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.at.desc())
            .limit(limit)
            .all()
        )
        # 倒序取回后反转为时间正序
        return [
            {"role": m.role, "content": m.content}
            for m in reversed(messages)
        ]
    finally:
        db.close()


def build_reply(user_id: str, message: str) -> tuple[str, list[dict], bool]:
    """编排一次对话回复：查订单 → 检索手册 → 生成带来源回答。

    返回 (reply, sources, suggest_ticket)。sources 里带有的 ``handle``
    就是回答中引用用的编号，前端可据此回查原文。
    """
    conversation_id = get_or_create_conversation(user_id)
    context = history(conversation_id)

    # 匿名用户无订单，直接跳过「查订单」环节
    order = orders.query_order(user_id) if user_id != "anonymous" else None
    matched = knowledge.search_knowledge(message)

    # 为本次检索到的资料分配请求内有效的短句柄，模型只看到编号
    registry = citations.SourceRegistry()
    prompt_items = citations.build_prompt_items(matched, registry)

    reply = llm.chat_reply(message, context, prompt_items, order, _chat_provider)

    # 把模型输出里的编号还原成可读的来源标注；
    # 编造或未注册的编号会被丢弃，避免出现无出处的引用
    reply = citations.decode_refs(reply, registry)

    append_message(conversation_id, "user", message)
    append_message(conversation_id, "assistant", reply)

    if matched:
        append_tried_step(user_id, f"按《{matched[0]['title']}》排查")

    suggest = any(
        k in message
        for k in ("失败", "报错", "不行", "解决不了", "无效", "工单", "不能用", "退款")
    )
    sources = citations.public_sources(registry)
    return reply, sources, suggest
