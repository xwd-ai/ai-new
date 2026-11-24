"""公共错误响应与模型配置健康报告。"""

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    detail: str | list[dict] = Field(description="业务错误为字符串，输入校验错误为详情数组")


class ModelCapability(BaseModel):
    capability: str
    available: bool = Field(description="仅表示配置检查通过，未验证远程服务")
    hint: str


class ModelCapabilityReport(BaseModel):
    all_available: bool
    capabilities: list[ModelCapability]


class HealthResponse(BaseModel):
    status: str
    # 是否支持流式对话（POST /api/chat/stream）。前端据此决定用流式还是
    # 一次性接口，避免「先盲试流式、失败再回退」多发一个请求。
    stream: bool = True
    models: ModelCapabilityReport
