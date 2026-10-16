from fastapi import APIRouter
from pydantic import BaseModel

chat_router = APIRouter()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


@chat_router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    return ChatResponse(reply=f"收到你的问题：{req.message}")
