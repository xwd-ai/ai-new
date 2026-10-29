# 契约层：集中定义 API 请求/响应的 Pydantic 模型，供路由层引用。
# 各领域契约拆分到子模块，此处统一 re-export。

from backend.schemas.auth import LoginRequest, LoginResponse
from backend.schemas.chat import ChatRequest, ChatResponse, ConversationResponse, SourceDetail, SourceSummary
from backend.schemas.common import ErrorResponse, HealthResponse
from backend.schemas.knowledge import (
    IngestRequest, IngestResponse, KnowledgeDiagnosisResponse, KnowledgeListResponse,
)
from backend.schemas.ticket import CreateTicketRequest, ReplyRequest

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ConversationResponse",
    "CreateTicketRequest",
    "ErrorResponse",
    "HealthResponse",
    "IngestRequest",
    "IngestResponse",
    "KnowledgeDiagnosisResponse",
    "KnowledgeListResponse",
    "LoginRequest",
    "LoginResponse",
    "ReplyRequest",
    "SourceDetail",
    "SourceSummary",
]
