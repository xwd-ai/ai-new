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
    models: ModelCapabilityReport
