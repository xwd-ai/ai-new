"""三模块统一错误响应结构与错误码测试。

锁定四件事：
1. 响应体只由 build_error_body 组装，字段固定为 error / error_code /
   error_detail（可选），且 error_detail 只在有补充上下文时出现；
2. 认证、订单、工单三个异常族的 to_payload 与统一构造入口逐字节一致，
   不会因为模块各自演化出现字段名或可选字段的分歧；
3. 错误码符合「<模块>_<原因>」约定，三族之间不重名，模块前缀与所属模块一致；
4. 路由层用同一个基类收口：状态码取自异常自带的 http_status，工单路由把
   认证异常翻译成本模块错误码，无法归类的认证异常按原样抛出而不是硬套错误码。

测试只构造内存对象与临时 FastAPI 应用，不读写真实的 app.db。
"""

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from backend.api.auth import (
    AuthErrorRoute,
    BusinessErrorRoute,
    build_error_body,
    error_payload,
    error_response,
    is_valid_error_code,
)
from backend.api.tickets import TicketErrorRoute
from backend.models import order as order_models
from backend.models import ticket as ticket_models
from backend.services import auth as auth_service


# ---------------------------------------------------------------------------
# 辅助：收集三族异常与全部错误码
# ---------------------------------------------------------------------------


def _auth_error_classes() -> list[type]:
    """认证模块里所有 AuthError 派生类（按类名排序，保证遍历顺序稳定）。"""
    classes = [
        obj
        for obj in vars(auth_service).values()
        if isinstance(obj, type)
        and issubclass(obj, auth_service.AuthError)
        and obj is not auth_service.AuthError
    ]
    return sorted(classes, key=lambda cls: cls.__name__)


def _code_values(holder: type, prefix: str) -> list[str]:
    """取错误码容器类上以指定前缀开头的字符串常量。"""
    return [
        value
        for value in vars(holder).values()
        if isinstance(value, str) and value.startswith(prefix)
    ]


def _sample_errors() -> list[Exception]:
    """三族各取若干能触发不同分支的代表性异常。"""
    return [
        # 认证：带补充上下文与不带的两种
        auth_service.InvalidUsernameError("用户名不合法", {"rule": "charset"}),
        auth_service.WeakPasswordError("密码强度不足"),
        auth_service.TokenMissingError("缺少认证令牌"),
        # 工单：无权限、不存在、非法流转
        ticket_models.TicketPermissionError(),
        ticket_models.TicketNotFoundError("T-0001"),
        ticket_models.TicketTransitionError("closed", "open"),
        # 订单：不存在、查询条件不可用、非法流转
        order_models.OrderNotFoundError("O-1001"),
        order_models.OrderQueryError("查询条件不可用", details={"field": "status"}),
        order_models.OrderStatusTransitionError("active", "unknown"),
    ]


# ---------------------------------------------------------------------------
# 一、响应体构造
# ---------------------------------------------------------------------------


def test_错误体字段顺序固定且无补充上下文时不出现_detail():
    body = build_error_body("TICKET_NOT_FOUND", "工单不存在")
    assert list(body) == ["error", "error_code"]
    assert body == {"error": "工单不存在", "error_code": "TICKET_NOT_FOUND"}


def test_错误体在_http_status_之外还接受补充上下文():
    body = build_error_body("ORDER_INVALID_FILTER", "查询条件不可用", {"field": "status"})
    assert list(body) == ["error", "error_code", "error_detail"]
    assert body["error_detail"] == {"field": "status"}


def test_空补充上下文与空字典都不产生_detail_字段():
    assert "error_detail" not in build_error_body("AUTH_ERROR", "失败", {})
    assert "error_detail" not in build_error_body("AUTH_ERROR", "失败", None)


def test_补充上下文被复制_调用方后续修改不影响已生成响应():
    details = {"ticket_id": "T-0001"}
    body = build_error_body("TICKET_NOT_FOUND", "工单不存在", details)
    details["ticket_id"] = "T-9999"
    assert body["error_detail"] == {"ticket_id": "T-0001"}


def test_文案为空时仍返回空串而不是缺字段():
    body = build_error_body("TICKET_INVALID_PAYLOAD", "")
    assert body["error"] == ""


# ---------------------------------------------------------------------------
# 二、错误码约定
# ---------------------------------------------------------------------------


def test_三个模块的全部错误码都符合命名约定():
    codes = _code_values(ticket_models.TicketErrorCode, "TICKET_")
    codes += _code_values(order_models.OrderErrorCode, "ORDER_")
    codes += [cls.error_code for cls in _auth_error_classes()]
    assert len(codes) >= 20
    invalid = [code for code in codes if not is_valid_error_code(code)]
    assert invalid == []


@pytest.mark.parametrize(
    "code",
    ["", "ticket_not_found", "1TICKET", "TICKET-NOT-FOUND", " TICKET_NOT_FOUND", None],
)
def test_不符合约定的错误码被识别出来(code):
    assert is_valid_error_code(code) is False


def test_错误码在三族之间不重名():
    codes = _code_values(ticket_models.TicketErrorCode, "TICKET_")
    codes += _code_values(order_models.OrderErrorCode, "ORDER_")
    codes += [cls.error_code for cls in _auth_error_classes()]
    assert len(codes) == len(set(codes))


def test_错误码前缀与所属模块一致():
    assert all(
        code.startswith("TICKET_")
        for code in _code_values(ticket_models.TicketErrorCode, "TICKET_")
    )
    assert all(
        code.startswith("ORDER_")
        for code in _code_values(order_models.OrderErrorCode, "ORDER_")
    )
    assert all(cls.error_code.startswith("AUTH_") for cls in _auth_error_classes())


# ---------------------------------------------------------------------------
# 三、三个异常族与统一构造入口一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", _sample_errors(), ids=lambda e: type(e).__name__)
def test_异常自带_payload_与统一构造入口一致(exc):
    assert exc.to_payload() == build_error_body(
        exc.code, exc.message, exc.details
    )


@pytest.mark.parametrize("exc", _sample_errors(), ids=lambda e: type(e).__name__)
def test_统一取值的四个属性在三族上都可读(exc):
    assert isinstance(exc.code, str) and exc.code
    assert isinstance(exc.message, str) and exc.message
    assert isinstance(exc.details, dict)
    assert isinstance(exc.http_status, int)
    assert error_payload(exc)["error_code"] == exc.code


def test_认证异常的旧字段名与新字段名指向同一份数据():
    exc = auth_service.InvalidUsernameError("用户名不合法", {"rule": "charset"})
    assert exc.code == exc.error_code
    assert exc.details == exc.detail


def test_工单与订单异常的状态码约定():
    assert ticket_models.TicketAuthRequiredError().http_status == 401
    assert ticket_models.TicketPermissionError().http_status == 403
    assert ticket_models.TicketNotFoundError().http_status == 404
    assert ticket_models.TicketRuleError().http_status == 409
    assert order_models.OrderNotFoundError().http_status == 404
    assert order_models.OrderQueryError().http_status == 400
    assert order_models.OrderStatusTransitionError("active", "expired").http_status == 409


# ---------------------------------------------------------------------------
# 四、路由基类收口
# ---------------------------------------------------------------------------


def _app_for(route_class: type, exc: Exception) -> FastAPI:
    """构造一个只挂一条会抛异常接口的应用，用于验证路由基类的转换结果。"""
    app = FastAPI()
    router = APIRouter(route_class=route_class)

    @router.get("/boom")
    def boom():
        raise exc

    app.include_router(router)
    return app


def _client(route_class: type, exc: Exception) -> TestClient:
    return TestClient(_app_for(route_class, exc), raise_server_exceptions=False)


def test_错误响应状态码取自异常而不是固定值():
    assert _client(AuthErrorRoute, auth_service.TokenMissingError("缺少认证令牌")).get("/boom").status_code == 401
    assert _client(TicketErrorRoute, ticket_models.TicketNotFoundError("T-0001")).get("/boom").status_code == 404
    assert _client(TicketErrorRoute, order_models.OrderQueryError("条件不可用")).get("/boom").status_code == 400


def test_三个异常族经路由后响应结构完全相同():
    bodies = [
        _client(
            AuthErrorRoute,
            auth_service.InvalidUsernameError("用户名不合法", {"rule": "charset"}),
        ).get("/boom").json(),
        _client(TicketErrorRoute, ticket_models.TicketNotFoundError("T-0001")).get("/boom").json(),
        _client(TicketErrorRoute, order_models.OrderNotFoundError("O-1001")).get("/boom").json(),
    ]
    for body in bodies:
        assert list(body) == ["error", "error_code", "error_detail"]
    assert [body["error_code"] for body in bodies] == [
        "AUTH_INVALID_USERNAME",
        "TICKET_NOT_FOUND",
        "ORDER_NOT_FOUND",
    ]


def test_无补充上下文的异常经路由后不带_detail_字段():
    body = _client(TicketErrorRoute, ticket_models.TicketPermissionError()).get("/boom").json()
    assert body == {"error": "无权限", "error_code": "TICKET_FORBIDDEN"}


def test_工单路由把认证异常翻译成本模块错误码():
    body = _client(TicketErrorRoute, auth_service.TokenMissingError("缺少认证令牌")).get("/boom").json()
    assert body == {"error": "未登录", "error_code": "TICKET_UNAUTHORIZED"}
    denied = _client(TicketErrorRoute, auth_service.PermissionDeniedError("无权操作")).get("/boom").json()
    assert denied == {"error": "无权限", "error_code": "TICKET_FORBIDDEN"}


def test_无法归类的认证异常不被硬套错误码():
    # 注册类异常不属于「未登录/无权限」，翻译函数返回 None，按原样抛出由上层处理
    response = _client(TicketErrorRoute, auth_service.WeakPasswordError("密码强度不足")).get("/boom")
    assert response.status_code == 500


def test_认证路由不改写认证异常的错误码():
    body = _client(AuthErrorRoute, auth_service.PermissionDeniedError("无权操作")).get("/boom").json()
    assert body == {"error": "无权操作", "error_code": "AUTH_FORBIDDEN"}


def test_error_response_对没有状态码与错误码的异常给出兜底值():
    response = error_response(ValueError("未知失败"))
    assert response.status_code == 500
    assert response.body == b'{"error":"","error_code":""}'


def test_基类不接管业务异常():
    # error_types 为空表示不接管，异常原样冒泡，避免误吞未声明的错误
    assert BusinessErrorRoute.error_types == ()
    response = _client(BusinessErrorRoute, ticket_models.TicketNotFoundError("T-0001")).get("/boom")
    assert response.status_code == 500
