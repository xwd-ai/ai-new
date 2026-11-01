from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query

from backend.database import SessionLocal
from backend.services import chat as chat_service, knowledge
from backend.models import Conversation, Message
from backend.schemas import (
    ChatRequest, ChatResponse, ConversationResponse, ErrorResponse,
    KnowledgeDiagnosisResponse, SourceDetail,
)

chat_router = APIRouter()


@chat_router.post(
    "/chat", response_model=ChatResponse, summary="生成对话回答",
    description="允许匿名请求；无效登录态按匿名处理。模型调用失败仍返回 200，故障提示见 reply 与 trace。",
)
def chat(req: ChatRequest, authorization: str = Header(default="")) -> ChatResponse:
    with chat_service.execution_scope():
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
    "/knowledge/diagnose", response_model=KnowledgeDiagnosisResponse, summary="诊断知识检索",
    responses={403: {"model": ErrorResponse, "description": "未登录或不具备管理员权限"}},
)
def diagnose_knowledge(
    q: str,
    top_k: Annotated[int, Query(description="实际截断到 1～20，不因整数超出范围返回 422")] = 3,
    authorization: str = Header(default=""),
) -> dict:
    """诊断一次检索，返回每个阶段的命中情况。

    用于排查「为什么搜不到 / 为什么返回的是这些」：直接看出是没召回、
    被融合挤掉、被阈值拦下，还是被去冗余合并了。

    管理员专属：它会把内部标识与打分过程暴露出来，不适合给普通用户。
    """
    user = chat_service.current_user(authorization)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return knowledge.diagnose(q, top_k=max(1, min(top_k, 20)))


@chat_router.get(
    "/conversation", response_model=ConversationResponse, summary="读取最近会话的消息",
    description="未登录或登录态无效时返回空消息列表；有效用户仅返回其最近会话。",
)
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
