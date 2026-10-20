from fastapi import APIRouter, Header
from pydantic import BaseModel

from backend import chat as chat_service
from backend.database import SessionLocal
from backend.models import Conversation, Message

chat_router = APIRouter()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    suggest_ticket: bool = False
    sources: list = []


@chat_router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
    user = chat_service.current_user(authorization)
    user_id = user["id"] if user else "anonymous"

    reply, sources, suggest = chat_service.build_reply(user_id, req.message)

    return ChatResponse(reply=reply, suggest_ticket=suggest, sources=sources)


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
