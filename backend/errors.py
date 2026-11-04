"""把服务异常归类为稳定的错误码与可对外展示的提示。"""

from dataclasses import dataclass
import sys


class ModelConfigurationError(ValueError):
    """模型配置缺失或仍在使用占位值。"""


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    retryable: bool = False


def classify_error(exc: Exception) -> ErrorInfo:
    """按异常类型分类，不把上游响应、密钥或内部路径拼进提示。"""
    if isinstance(exc, TimeoutError):
        return ErrorInfo("timeout", "服务响应超时，请稍后重试。", True)
    if isinstance(exc, ConnectionError):
        return ErrorInfo("connection_error", "暂时无法连接服务，请稍后重试。", True)
    if isinstance(exc, ModelConfigurationError):
        return ErrorInfo("model_configuration", "模型服务配置有误，请联系管理员检查。")
    # 真实 SDK 异常只会在 SDK 已加载后产生。普通故障不触发整套 SDK 导入。
    if sys.modules.get('openai') is None:
        return ErrorInfo("internal_error", "处理请求时发生错误，请稍后重试或联系管理员。")
    from openai import (
        APIConnectionError, APIStatusError, APITimeoutError,
        AuthenticationError, PermissionDeniedError, RateLimitError,
    )
    # SDK 的超时异常也是连接异常，必须先判断超时。
    if isinstance(exc, APITimeoutError):
        return ErrorInfo("timeout", "服务响应超时，请稍后重试。", True)
    if isinstance(exc, APIConnectionError):
        return ErrorInfo("connection_error", "暂时无法连接服务，请稍后重试。", True)
    if isinstance(exc, RateLimitError):
        return ErrorInfo("rate_limited", "服务请求繁忙，请稍后重试。", True)
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return ErrorInfo("model_configuration", "模型服务配置有误，请联系管理员检查。")
    if isinstance(exc, APIStatusError):
        if exc.status_code >= 500:
            return ErrorInfo("upstream_unavailable", "模型服务暂时不可用，请稍后重试。", True)
        return ErrorInfo("model_request_rejected", "模型服务拒绝了本次请求，请联系管理员检查。")
    return ErrorInfo("internal_error", "处理请求时发生错误，请稍后重试或联系管理员。")
