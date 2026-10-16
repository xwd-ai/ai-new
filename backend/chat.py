from fastapi import APIRouter
from pydantic import BaseModel

from backend import llm

chat_router = APIRouter()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


@chat_router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    reply = llm.chat_reply(req.message)
    return ChatResponse(reply=reply)
