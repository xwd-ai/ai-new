import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from backend.api.auth import current_user
from backend.database import SessionLocal
from backend.services import chat as chat_service, knowledge
from backend.models import Conversation, Message
from backend.schemas import (
    ChatRequest, ChatResponse, ConversationResponse, ErrorResponse,
    SourceDetail,
)

chat_router = APIRouter()


@chat_router.post(
    "/chat", response_model=ChatResponse, summary="生成对话回答",
    description="允许匿名请求；无效登录态按匿名处理。模型调用失败仍返回 200，故障提示见 reply 与 trace。",
)
def chat(req: ChatRequest, user: dict | None = Depends(current_user)) -> ChatResponse:
    with chat_service.execution_scope():
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


@chat_router.get(
    "/sources/{source_id}", response_model=SourceDetail, summary="回查知识来源原文",
    responses={404: {"model": ErrorResponse, "description": "来源不存在"}},
)
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


@chat_router.get(
    "/conversation", response_model=ConversationResponse, summary="读取最近会话的消息",
    response_model_exclude_none=True,
    description="有效用户仅返回最近会话。分页时附带 total，选中会话时附带 conversation_id 和 history_revision。无效登录态返回空历史。",
)
def conversation(
    user: dict | None = Depends(current_user),
    offset: Annotated[int | None, Query(ge=0, description="跳过的消息数，省略时为 0")] = None,
    limit: Annotated[int | None, Query(ge=1, le=100, description="最多返回条数，分页时默认 100")] = None,
):
    paged = offset is not None or limit is not None
    empty = {"messages": [], **({"total": 0} if paged else {})}
    if not user:
        return empty
    db = SessionLocal()
    try:
        db.execute(text('BEGIN'))
        conv = (
            db.query(Conversation)
            .filter(Conversation.user_id == user["id"])
            .order_by(
                Conversation.updated_at.desc(), Conversation.created_at.desc(), Conversation.id.desc(),
            )
            .first()
        )
        if not conv:
            return empty
        query = (
            db.query(Message.role, Message.content, Message.at, Message.id)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.at.asc(), Message.id.asc())
        )
        pagination = {}
        if paged:
            start = 0 if offset is None else offset
            stop = start + (100 if limit is None else limit)
            digest = hashlib.sha256()
            messages = []
            total = 0
            # 同一次有序读取生成指纹、计数和分页窗口。
            for index, row in enumerate(query.yield_per(100)):
                digest.update(json.dumps(tuple(row), separators=(",", ":")).encode("ascii"))
                digest.update(b"\n")
                if start <= index < stop:
                    messages.append(row)
                total += 1
            pagination = {
                "total": total,
                "conversation_id": conv.id,
                "history_revision": digest.hexdigest(),
            }
        else:
            messages = query.all()
        return {
            "messages": [
                {"role": m.role, "content": m.content, "at": m.at} for m in messages
            ],
            **pagination,
        }
    finally:
        db.close()
