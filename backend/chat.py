from fastapi import APIRouter
from pydantic import BaseModel

from backend import llm

chat_router = APIRouter()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    suggest_ticket: bool = False


@chat_router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    reply = llm.chat_reply(req.message)
    # 简单启发式：命中「失败/报错/不行/解决不了/工单」等则建议建单
    suggest = any(k in req.message for k in ("失败", "报错", "不行", "解决不了", "无效", "工单", "不能用"))
    return ChatResponse(reply=reply, suggest_ticket=suggest)
