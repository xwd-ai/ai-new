"""Go 对话服务使用的内部 Agent 接口。"""

import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from backend.api.agent_stream import stream_response
from backend.api.ratelimit import enforce_chat_rate
from backend.services import chat

agent_router = APIRouter(prefix="/internal/agent", include_in_schema=False)


def require_agent_token(x_agent_token: str = Header(default="")) -> None:
    expected = os.getenv("AGENT_INTERNAL_TOKEN", "").strip()
    if not expected or not hmac.compare_digest(x_agent_token.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="无权访问 Agent 内部接口")


class HistoryMessage(BaseModel):
    role: str
    content: str


class AgentRequest(BaseModel):
    user_id: str
    message: str
    history: list[HistoryMessage] = Field(max_length=8)


@agent_router.post("/reply", dependencies=[Depends(require_agent_token)])
def reply(req: AgentRequest):
    enforce_chat_rate(req.user_id)
    text, sources, suggest, step, (summary, _) = chat.generate_reply(
        req.user_id, req.message, [item.model_dump() for item in req.history],
    )
    return {
        "reply": text, "sources": sources, "suggest_ticket": suggest,
        "trace": summary.to_dict()["events"], "tried_step": step,
    }


@agent_router.post("/stream", dependencies=[Depends(require_agent_token)])
def stream(req: AgentRequest):
    enforce_chat_rate(req.user_id)
    messages, sources, suggest = chat.prepare_stream(
        req.user_id, req.message, [item.model_dump() for item in req.history],
    )
    return stream_response(messages, sources, suggest)
