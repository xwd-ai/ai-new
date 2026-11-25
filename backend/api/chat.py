import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from backend.api.auth import current_user
from backend.api.ratelimit import chat_rate_limit
from backend.database import SessionLocal
from backend.services import chat as chat_service, knowledge, llm
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
def chat(
    req: ChatRequest,
    user: dict | None = Depends(current_user),
    _rate: None = Depends(chat_rate_limit),
) -> ChatResponse:
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


def _sse_frame(event: str, payload: dict) -> str:
    """把一条事件编码成 SSE 帧：event 行 + data 行 + 结束空行。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@chat_router.post(
    "/chat/stream",
    summary="流式生成对话回答",
    description=(
        "SSE 输出：先推检索到的来源（event: retrieval），"
        "再逐段推模型回复增量（event: delta），最后推完整回答（event: done）。"
        "不做 Agent 多轮工具决策，供需要即时反馈的场景使用。"
    ),
)
def chat_stream_endpoint(
    req: ChatRequest,
    user: dict | None = Depends(current_user),
    _rate: None = Depends(chat_rate_limit),
) -> StreamingResponse:
    """流式回答：检索一次完成，模型输出边生成边推送。

    与 /chat 的分工：/chat 走 Agent 循环，回答质量更高但必须等全部生成完；
    本接口牺牲多轮决策换取即时反馈，适合对话类前端。
    """
    user_id = user["id"] if user else "anonymous"
    messages, sources, conversation_id, suggest = chat_service.build_stream_messages(
        user_id, req.message
    )

    return stream_response(
        messages, sources, suggest,
        lambda reply: chat_service.append_turn(conversation_id, req.message, reply),
    )


def stream_response(messages, sources, suggest, save=None) -> StreamingResponse:
    def event_stream():
        # 来源先发：前端在第一个字到达前就能渲染证据列表
        yield _sse_frame("retrieval", {"sources": sources})

        collected: list[str] = []
        try:
            for delta in llm.chat_stream_messages(messages):
                collected.append(delta)
                yield _sse_frame("delta", {"text": delta})
        except Exception as exc:  # noqa: BLE001 - 流已开始，只能以事件形式报告
            yield _sse_frame("error", {"message": f"生成中断：{exc}"})
            return

        reply = "".join(collected)
        # 生成完成后才落库：中途断流不会留下半截回答
        if save is not None:
            save(reply)
        yield _sse_frame(
            "done", {"reply": reply, "suggest_ticket": suggest}
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 让反向代理不要缓冲，否则客户端会攒一大块才收到
            "X-Accel-Buffering": "no",
        },
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
    response: Response,
    user: dict | None = Depends(current_user),
    offset: Annotated[int | None, Query(ge=0, description="跳过的消息数，省略时为 0")] = None,
    limit: Annotated[int | None, Query(ge=1, le=100, description="最多返回条数，分页时默认 100")] = None,
):
    response.headers["Cache-Control"] = "no-store"
    paged = offset is not None or limit is not None
    empty = {"messages": [], **({"total": 0} if paged else {})}
    if not user:
        return empty
    db = SessionLocal()
    try:
        db.execute(text('BEGIN'))
        if paged:
            connection = db.connection()
            # 先固定读取快照。data_version 检测其他连接的提交，
            # total_changes 补足当前物理连接执行的写入。
            version = connection.exec_driver_sql("PRAGMA main.data_version").scalar_one()
            changes = connection.connection.driver_connection.total_changes
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
            size = 100 if limit is None else limit
            cache_key = (version, changes, conv.id)
            cached = connection.info.get("conversation_history_revision")
            if cached is not None and cached[0] == cache_key:
                _, total, revision = cached
                messages = query.offset(start).limit(size).all() if start < total else []
            else:
                stop = start + size
                digest = hashlib.sha256()
                messages = []
                total = 0
                for index, row in enumerate(query.yield_per(100)):
                    digest.update(json.dumps(tuple(row), separators=(",", ":")).encode("ascii"))
                    digest.update(b"\n")
                    if start <= index < stop:
                        messages.append(row)
                    total += 1
                revision = digest.hexdigest()
                connection.info["conversation_history_revision"] = (cache_key, total, revision)
            pagination = {
                "total": total,
                "conversation_id": conv.id,
                "history_revision": revision,
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
