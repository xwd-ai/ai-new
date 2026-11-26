"""SSE 响应与模型增量的编码。"""

import json

from fastapi.responses import StreamingResponse

from backend.services import llm


def _sse_frame(event: str, payload: dict) -> str:
    """把一条事件编码成 SSE 帧：event 行 + data 行 + 结束空行。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
