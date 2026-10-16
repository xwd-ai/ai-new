import time

from fastapi import APIRouter, Header
from pydantic import BaseModel

from backend import auth, llm, store

chat_router = APIRouter()


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    suggest_ticket: bool = False


def _current_user(authorization: str):
    token = authorization.replace("Bearer ", "").strip()
    session = auth.get_session(token)
    if not session:
        return None
    return auth.find_by_username(session["username"])


def _append_message(user_id: str, role: str, content: str) -> None:
    data = store.read()
    data["conversations"].setdefault(user_id, [])
    data["conversations"][user_id].append(
        {"role": role, "content": content, "at": time.time()}
    )
    store.write(data)


@chat_router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
    user = _current_user(authorization)
    user_id = user["id"] if user else "anonymous"

    # 拉取该用户历史，作为上下文（demo 阶段只带最近 8 条）
    data = store.read()
    history = data["conversations"].get(user_id, [])[-8:]
    context = [{"role": m["role"], "content": m["content"]} for m in history]

    reply = llm.chat_reply(req.message, context)

    # 持久化：用户消息 + AI 回复
    _append_message(user_id, "user", req.message)
    _append_message(user_id, "assistant", reply)

    suggest = any(k in req.message for k in ("失败", "报错", "不行", "解决不了", "无效", "工单", "不能用"))
    return ChatResponse(reply=reply, suggest_ticket=suggest)


@chat_router.get("/conversation")
def conversation(authorization: str = Header(default="")):
    user = _current_user(authorization)
    if not user:
        return {"messages": []}
    data = store.read()
    messages = data["conversations"].get(user["id"], [])
    return {"messages": messages}
