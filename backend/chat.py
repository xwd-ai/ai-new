import time
import uuid

from fastapi import APIRouter, Header
from pydantic import BaseModel

from backend import auth, knowledge, llm, orders, store
from backend.database import SessionLocal
from backend.models import Conversation, Message

chat_router = APIRouter()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    suggest_ticket: bool = False
    sources: list = []


def _current_user(authorization: str):
    token = authorization.replace("Bearer ", "").strip()
    session = auth.get_session(token)
    if not session:
        return None
    return auth.find_by_username(session["username"])


def _append_message(conversation_id: str, role: str, content: str) -> None:
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


def _append_tried_step(user_id: str, step: str) -> None:
    """记录一次「已尝试步骤」，供后续建单沉淀。"""
    data = store.read()
    data.setdefault("tried_steps", {})
    data["tried_steps"].setdefault(user_id, [])
    data["tried_steps"][user_id].append(step)
    store.write(data)


def _get_tried_steps(user_id: str) -> str:
    data = store.read()
    steps = data.get("tried_steps", {}).get(user_id, [])
    return " · ".join(steps) if steps else ""


def _get_or_create_conversation(user_id: str) -> str:
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


def _history(conversation_id: str, limit: int = 8) -> list[dict]:
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


@chat_router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
    user = _current_user(authorization)
    user_id = user["id"] if user else "anonymous"

    # 1. 拉取历史上下文
    conversation_id = _get_or_create_conversation(user_id)
    context = _history(conversation_id)

    # 2. 查询订单与服务范围（Agent 的「查订单」工具）
    order = orders.query_order(user_id) if user else None

    # 3. 检索知识库（Agent 的「检索手册」工具）
    matched = knowledge.search_knowledge(req.message)

    # 4. 生成带来源的回答
    reply = llm.chat_reply(req.message, context, matched, order)

    # 5. 持久化消息
    _append_message(conversation_id, "user", req.message)
    _append_message(conversation_id, "assistant", reply)

    # 记录已尝试步骤（命中知识则说明给了排查建议）
    if matched:
        _append_tried_step(user_id, f"按《{matched[0]['title']}》排查")

    # 6. 结构化返回
    suggest = any(k in req.message for k in ("失败", "报错", "不行", "解决不了", "无效", "工单", "不能用", "退款"))
    sources = [
        {"id": k["id"], "title": k["title"], "kind": k["kind"]} for k in matched
    ]
    return ChatResponse(reply=reply, suggest_ticket=suggest, sources=sources)


@chat_router.get("/conversation")
def conversation(authorization: str = Header(default="")):
    user = _current_user(authorization)
    if not user:
        return {"messages": []}
    db = SessionLocal()
    try:
        conv = (
            db.query(Conversation)
            .filter(Conversation.user_id == user["id"])
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if not conv:
            return {"messages": []}
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.at.asc())
            .all()
        )
        return {
            "messages": [
                {"role": m.role, "content": m.content, "at": m.at} for m in messages
            ]
        }
    finally:
        db.close()
