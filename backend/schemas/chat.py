from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(description="本轮用户消息", examples=["导出失败怎么办？"])


class SourceSummary(BaseModel):
    """回答中的来源，使用 id 回查原文；handle 仅在本轮有效。"""

    id: str = Field(description="知识条目编号，用于 /api/sources/{source_id}")
    handle: str = Field(description="本轮引用句柄，例如 c1")
    title: str
    kind: str


class ChatResponse(BaseModel):
    reply: str
    suggest_ticket: bool = False
    sources: list[SourceSummary] = Field(default_factory=list, description="本轮注册的知识来源，可为空")
    # 执行过程事件：前端可据此展示「检索了什么、跑了几轮」
    trace: list[dict] = Field(
        default_factory=list,
        description="一次性返回的事件数组；共有 kind、iteration，其余字段随事件类型变化，非流式输出",
    )


class ConversationMessage(BaseModel):
    role: str
    content: str
    at: float = Field(description="Unix 时间戳，单位秒")


class ConversationResponse(BaseModel):
    messages: list[ConversationMessage] = Field(default_factory=list, description="最近会话的消息，按时间及编号升序；分页时为指定窗口")
    total: int | None = Field(default=None, ge=0, description="分页时返回最近会话的消息总数；无有效用户或会话时为 0")
    conversation_id: str | None = Field(default=None, description="分页时选中的最近会话标识；未选中会话时省略")


class SourceDetail(BaseModel):
    """来源详情：供前端点击回答中的来源标注后回查原文。"""

    id: str
    title: str
    kind: str
    content: str
    keywords: list[str] = []
