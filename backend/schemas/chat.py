from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    suggest_ticket: bool = False
    sources: list = []
    # 执行过程事件：前端可据此展示「检索了什么、跑了几轮」
    trace: list = []


class SourceDetail(BaseModel):
    """来源详情：供前端点击回答中的来源标注后回查原文。"""

    id: str
    title: str
    kind: str
    content: str
    keywords: list[str] = []
