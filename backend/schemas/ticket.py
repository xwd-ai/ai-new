from pydantic import BaseModel, Field, field_validator


class CreateTicketRequest(BaseModel):
    """建单请求：描述必填且非空白，其余字段限长。"""

    description: str = Field(..., min_length=1, max_length=500, description="问题描述")
    tried_steps: str = Field(default="", max_length=1000, description="已尝试步骤")
    evidence: str = Field(default="", max_length=1000, description="排查依据")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("问题描述不能为空")
        return text

    @field_validator("tried_steps", "evidence")
    @classmethod
    def _strip_optional(cls, v: str) -> str:
        return (v or "").strip()


class ReplyRequest(BaseModel):
    ticket_id: str = Field(..., min_length=1, max_length=64)
    content: str = Field(..., min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("回复内容不能为空")
        return text


class TransferRequest(BaseModel):
    """转人工请求：工单号必填，转人工原因为可选说明。"""

    ticket_id: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(default="", max_length=500, description="转人工原因")

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, v: str) -> str:
        return (v or "").strip()


class ConfirmRequest(BaseModel):
    """工单确认请求：待确认草稿经此确认后正式进入待处理。"""

    ticket_id: str = Field(..., min_length=1, max_length=64)


class CancelRequest(BaseModel):
    """工单取消请求：待确认草稿经此取消后流转为已关闭，取消原因为可选说明。"""

    ticket_id: str = Field(..., min_length=1, max_length=64)
    reason: str = Field(default="", max_length=500, description="取消原因")

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, v: str) -> str:
        return (v or "").strip()
