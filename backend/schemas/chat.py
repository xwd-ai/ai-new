from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    suggest_ticket: bool = False
    sources: list = []


class SourceDetail(BaseModel):
    """来源详情：供前端点击回答中的来源标注后回查原文。"""

    id: str
    title: str
    kind: str
    content: str
    keywords: list[str] = []
