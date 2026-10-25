from fastapi import APIRouter, Header, HTTPException

from backend.database import SessionLocal
from backend.services import chat as chat_service, knowledge
from backend.models import Conversation, Message
from backend.schemas import ChatRequest, ChatResponse, SourceDetail

chat_router = APIRouter()


@chat_router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
    user = chat_service.current_user(authorization)
    user_id = user["id"] if user else "anonymous"

    reply, sources, suggest = chat_service.build_reply(user_id, req.message)

    summary = chat_service.last_summary()
    trace = summary.to_dict()["events"] if summary else []
    return ChatResponse(
        reply=reply,
        suggest_ticket=suggest,
        sources=sources,
        trace=trace,
    )


@chat_router.get("/sources/{source_id}", response_model=SourceDetail)
def source_detail(source_id: str) -> SourceDetail:
    """按来源编号回查原文，供回答中的来源标注点击后查看。

    只返回知识库条目本身的内容；不存在时返回 404，便于前端区分
    「来源已失效」与「加载失败」。
    """
    doc = knowledge.get_knowledge(source_id)
    if not doc:
        raise HTTPException(status_code=404, detail="来源不存在")
    return SourceDetail(
        id=doc["id"],
        title=doc["title"],
        kind=doc["kind"],
        content=doc["content"],
        keywords=doc.get("keywords", []),
    )


@chat_router.get("/conversation")
def conversation(authorization: str = Header(default="")):
    user = chat_service.current_user(authorization)
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
