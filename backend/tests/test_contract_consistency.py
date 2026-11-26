"""三模块接口契约一致性检查。

认证、订单、工单各自维护接口，但调用方看到的只有一套约定。本文件不测具体
业务分支（那些由各模块自己的用例覆盖），只把跨模块的共性钉成可执行断言：

1. 错误体结构：三个模块的业务异常都转成 {error, error_code, error_detail?}，
   文案非空、错误码按模块前缀区分、HTTP 状态落在约定集合内；
2. 错误码归属：AUTH_ / ORDER_ / TICKET_ 三族前缀各自独立，不互相借用；
3. 鉴权口径：匿名一律 401、越权一律 403，响应体与业务错误同构；
4. 分页口径：列表接口的 total / page / page_size / pages / has_more 互相对得上，
   默认页长与上限一致，越界页给空列表而不是报错；
5. 摘要文案：列表、详情、统计、判定都给非空 message，前端不必自己拼；
6. 时间口径：对外时间统一为秒级时间戳，避免同一屏里混着 ISO 字符串；
7. 入参越界：三个模块都返回 422，响应体形态差异在此显式记录。

全部用例使用独立临时 SQLite，不接触真实 app.db。
"""

import inspect
import math
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import auth as api_auth
from backend.tests.reference_api import tickets as api_tickets
from backend.database import Base
from backend.models import Order, Product, Session, User
from backend.models.order import OrderErrorCode, OrderNotFoundError, OrderQueryError
from backend.models.ticket import (
    TicketAuthRequiredError,
    TicketErrorCode,
    TicketNotFoundError,
    TicketPermissionError,
    TicketRuleError,
    TicketValidationError,
)
from backend.services import auth as auth_service
from backend.services import orders as orders_service
from backend.tests.reference_api import ticket_service as ticket_service

ADMIN = {"Authorization": "Bearer tk-con-admin"}
CUSTOMER = {"Authorization": "Bearer tk-con-customer"}
OTHER = {"Authorization": "Bearer tk-con-other"}

# 业务错误响应体的允许字段：多一个字段就意味着前端要多写一处兼容。
ERROR_ENVELOPE_KEYS = {"error", "error_code", "error_detail"}
# 三个模块共用的 HTTP 状态语义：401 身份、403 权限、404 资源、409 规则、422 入参。
ALLOWED_ERROR_STATUS = {400, 401, 403, 404, 409, 422, 500}
MODULE_PREFIXES = ("AUTH_", "ORDER_", "TICKET_")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 管理员/客户/另一客户三方身份，挂载认证与工单两个路由。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'contract.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, ticket_service, auth_service, orders_service):
        monkeypatch.setattr(module, "SessionLocal", factory)

    now = time.time()
    db = factory()
    try:
        db.add_all(
            [
                User(id="u-admin", username="con-admin", password_hash="x", name="客服小张", role="admin"),
                User(id="u-cust", username="con-cust", password_hash="x", name="客户小李", role="customer"),
                User(id="u-other", username="con-other", password_hash="x", name="客户老王", role="customer"),
                Session(token="tk-con-admin", user_id="u-admin", username="con-admin",
                        name="客服小张", role="admin", expires_at=now + 3600),
                Session(token="tk-con-customer", user_id="u-cust", username="con-cust",
                        name="客户小李", role="customer", expires_at=now + 3600),
                Session(token="tk-con-other", user_id="u-other", username="con-other",
                        name="客户老王", role="customer", expires_at=now + 3600),
                Product(id="pro", name="专业版", features=["导出", "批量处理"]),
                Order(id="O-CON-1", user_id="u-cust", product_tier="pro", status="active",
                      started_at=now - 86400, expires_at=now + 86400 * 30),
                Order(id="O-CON-2", user_id="u-cust", product_tier="pro", status="active",
                      started_at=now - 86400, expires_at=now + 86400 * 60),
                Order(id="O-CON-3", user_id="u-other", product_tier="pro", status="active",
                      started_at=now - 86400, expires_at=now + 86400 * 15),
            ]
        )
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.include_router(api_auth.auth_router, prefix="/api")
    app.include_router(api_tickets.tickets_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _ok(client, path, headers=CUSTOMER, method="get", **kwargs):
    response = getattr(client, method)(path, headers=headers, **kwargs)
    assert response.status_code == 200, f"{path} 应返回 200，实际 {response.status_code}：{response.text}"
    return response.json()


def _soft_seed(client, count: int) -> None:
    """建若干张草稿工单，让列表与统计有数据可对。"""
    for index in range(count):
        body = client.post(
            "/api/tickets",
            json={"description": f"一致性检查-{index}"},
            headers=CUSTOMER,
        ).json()
        assert body["created"] is True, body


def _assert_error_body(body: dict, family: str, path: str) -> None:
    """错误体必须落在统一结构里，且错误码属于期望的模块族。"""
    assert set(body) <= ERROR_ENVELOPE_KEYS, f"{path} 的错误体多出字段：{set(body) - ERROR_ENVELOPE_KEYS}"
    assert isinstance(body.get("error"), str) and body["error"].strip(), f"{path} 缺少可展示文案"
    code = body.get("error_code")
    assert isinstance(code, str) and code.startswith(family), f"{path} 的错误码不属于 {family}：{code}"
    if "error_detail" in body:
        assert isinstance(body["error_detail"], dict), f"{path} 的 error_detail 应为对象"


def _codes_of(*sources) -> set[str]:
    """从错误码常量类与异常类里收集全部错误码。

    常量类（TicketErrorCode / OrderErrorCode）按大写属性收集；
    认证侧的错误码分散在异常类上，按类的 error_code 属性收集。
    """
    codes: set[str] = set()
    for source in sources:
        if isinstance(source, type) and hasattr(source, "to_payload"):
            code = getattr(source, "error_code", None) or getattr(source, "code", None)
            if isinstance(code, str):
                codes.add(code)
            continue
        for name in dir(source):
            if name.startswith("_"):
                continue
            value = getattr(source, name)
            if isinstance(value, str) and value.isupper():
                codes.add(value)
    return codes


# ---------------------------------------------------------------------------
# 一、错误体结构
# ---------------------------------------------------------------------------


def test_业务错误体在三个模块保持同一结构():
    """三个模块的异常对象都自带 to_payload，结构与非空文案必须一致。"""
    samples = [
        ("AUTH_", auth_service.TokenMissingError("缺少认证令牌")),
        ("AUTH_", auth_service.PermissionDeniedError("无权限", {"role": "customer"})),
        ("AUTH_", auth_service.WeakPasswordError("密码过弱", {"violations": ["长度不足"]})),
        ("AUTH_", auth_service.UsernameTakenError("用户名已被占用")),
        ("TICKET_", TicketAuthRequiredError()),
        ("TICKET_", TicketPermissionError("无权限查看该工单")),
        ("TICKET_", TicketNotFoundError("T-9999")),
        ("TICKET_", TicketValidationError("页码必须大于等于 1")),
        ("TICKET_", TicketRuleError()),
        ("ORDER_", OrderNotFoundError("O-9999")),
        ("ORDER_", OrderQueryError("未知状态：paused", details={"field": "status"})),
    ]

    for family, exc in samples:
        payload = exc.to_payload()
        assert set(payload) <= ERROR_ENVELOPE_KEYS, f"{type(exc).__name__} 的错误体结构不符：{payload}"
        assert payload["error"].strip(), f"{type(exc).__name__} 没有可展示文案"
        assert payload["error_code"].startswith(family), f"{type(exc).__name__} 错误码族不符"
        assert getattr(exc, "http_status", None) in ALLOWED_ERROR_STATUS, (
            f"{type(exc).__name__} 的 HTTP 状态不在约定集合内：{getattr(exc, 'http_status', None)}"
        )


def test_错误码前缀分属三个模块且互不借用():
    """错误码是调用方的分支依据，AUTH_ / ORDER_ / TICKET_ 三族不能混用。"""
    auth_errors = [
        cls
        for _, cls in inspect.getmembers(auth_service, inspect.isclass)
        if issubclass(cls, auth_service.AuthError) and cls.__module__ == auth_service.__name__
    ]
    families = {
        "TICKET_": _codes_of(TicketErrorCode),
        "ORDER_": _codes_of(OrderErrorCode),
        "AUTH_": _codes_of(*auth_errors),
    }

    for prefix, codes in families.items():
        assert codes, f"{prefix} 族没有任何错误码"
        wrong = {code for code in codes if not code.startswith(prefix)}
        assert not wrong, f"{prefix} 族里混入了不属于自己的错误码：{wrong}"

    overlap = families["TICKET_"] & families["ORDER_"]
    assert not overlap, f"工单与订单共用了同一个错误码：{overlap}"
    assert not (families["AUTH_"] & (families["TICKET_"] | families["ORDER_"])), "认证错误码被其它模块复用"


# ---------------------------------------------------------------------------
# 二、鉴权口径
# ---------------------------------------------------------------------------


def test_未登录与越权响应在三模块同构(client):
    """匿名 401、越权 403 是三个模块共用的口径，响应体结构与业务错误一致。"""
    anonymous_cases = [
        ("/api/tickets", "TICKET_"),
        ("/api/order/list", "TICKET_"),
        ("/api/order/admin", "TICKET_"),
        ("/api/tickets/stats", "TICKET_"),
        ("/api/sessions/cleanup", "AUTH_"),
    ]
    for path, family in anonymous_cases:
        response = client.get(path) if path != "/api/sessions/cleanup" else client.post(path)
        assert response.status_code == 401, f"{path} 对匿名访问应返回 401，实际 {response.status_code}"
        _assert_error_body(response.json(), family, path)

    denied_cases = [
        ("/api/order/admin", "TICKET_"),
        ("/api/tickets/stats?user_id=u-other", "TICKET_"),
    ]
    for path, family in denied_cases:
        response = client.get(path, headers=CUSTOMER)
        assert response.status_code == 403, f"{path} 对越权访问应返回 403，实际 {response.status_code}"
        _assert_error_body(response.json(), family, path)

    cleanup = client.post("/api/sessions/cleanup", headers=CUSTOMER)
    assert cleanup.status_code == 403, cleanup.text
    _assert_error_body(cleanup.json(), "AUTH_", "/api/sessions/cleanup")


def test_当前用户接口的匿名返回是唯一豁免(client):
    """匿名访问 /api/me 返回 200 + user=null：这是三模块里唯一的空身份约定。

    其余接口对匿名一律 401，这条豁免必须继续成立，否则前端无法在启动时
    静默探测登录态；一旦有人把它改成 401，这里的断言会先报警。
    """
    body = _ok(client, "/api/me", headers={})
    assert set(body) == {"user"} and body["user"] is None

    logged_in = _ok(client, "/api/me", headers=CUSTOMER)["user"]
    assert logged_in["id"] == "u-cust"
    # 对外身份字段三模块共用：工单建单的 identity 与之同名同义
    created = client.post("/api/tickets", json={"description": "身份字段对照"}, headers=CUSTOMER).json()
    identity = created["identity"]
    for field in ("id", "name", "role"):
        assert identity[field] == logged_in[field], f"identity.{field} 与 /api/me 口径不一致"


# ---------------------------------------------------------------------------
# 三、列表与摘要契约
# ---------------------------------------------------------------------------


def test_列表分页元数据在工单与订单之间口径一致(client):
    """两个列表接口的公共分页元数据必须同名同算法，翻页组件才能复用。"""
    _soft_seed(client, 5)
    common = {"total", "page", "page_size", "pages", "has_more", "order", "statuses", "keyword", "message"}

    ticket_list = _ok(client, "/api/tickets", headers=ADMIN)
    admin_orders = _ok(client, "/api/order/admin", headers=ADMIN)
    assert common <= set(ticket_list), f"工单列表缺少公共分页字段：{common - set(ticket_list)}"
    assert common <= set(admin_orders), f"管理订单列表缺少公共分页字段：{common - set(admin_orders)}"

    for path, payload in (
        ("/api/tickets", ticket_list),
        ("/api/order/admin", admin_orders),
    ):
        assert payload["page"] == 1 and payload["page_size"] == 20, f"{path} 的默认分页口径不一致"
        assert payload["total"] >= 1, f"{path} 应至少有一条数据"
        assert payload["pages"] == math.ceil(payload["total"] / payload["page_size"])
        assert payload["has_more"] is False, f"{path} 首页不足以产生下一页时应为 false"

    small = _ok(client, "/api/tickets?page_size=2", headers=ADMIN)
    order_small = _ok(client, "/api/order/admin?page_size=2", headers=ADMIN)
    for payload in (small, order_small):
        assert payload["pages"] == math.ceil(payload["total"] / 2)
        assert payload["has_more"] is (payload["total"] > 2)

    # 越界页统一给空列表，且不再声称还有下一页
    for path in ("/api/tickets?page=99", "/api/order/admin?page=99"):
        payload = _ok(client, path, headers=ADMIN)
        items_key = "tickets" if "tickets" in payload else "items"
        assert payload[items_key] == [] and payload["has_more"] is False, f"{path} 的越界页语义不一致"


def test_列表与详情都带非空摘要(client):
    """message 是三个模块共用的「一句话结论」，缺失会让前端各写一套拼接。

    /api/order 是唯一例外：它按早期页面契约固定 7 个字段，不带 message。
    这条豁免写进断言而不是省略，避免日后再加接口时把它当成「可以不带摘要」的先例。
    """
    _soft_seed(client, 2)
    ticket_id = _ok(client, "/api/tickets", headers=CUSTOMER)["tickets"][0]["id"]

    paths = [
        "/api/tickets",
        "/api/tickets/pending",
        "/api/tickets/stats",
        f"/api/tickets/{ticket_id}",
        "/api/order/list",
        "/api/order/scope",
        "/api/order/feature-scope?feature=导出",
        "/api/order/renewal",
        "/api/order/admin",
        "/api/order/admin/O-CON-1",
    ]
    for path in paths:
        body = _ok(client, path, headers=ADMIN if path.startswith("/api/order/admin") else CUSTOMER)
        message = body.get("message")
        assert isinstance(message, str) and message.strip(), f"{path} 缺少非空 message"
        assert len(message) <= 200, f"{path} 的 message 过长（{len(message)} 字），不适合直接展示"

    # 历史契约：/api/order 只有 7 个展示字段，不含 message
    legacy = _ok(client, "/api/order", headers=CUSTOMER)["order"]
    assert "message" not in legacy and set(legacy) == {
        "has_order", "order_id", "product_tier", "product_name",
        "in_service", "features", "expires_at",
    }


def test_对外时间字段统一为秒级时间戳(client):
    """同一屏里既有创建时间又有到期时间，单位与类型必须一致。"""
    _soft_seed(client, 1)
    now = time.time()
    year = 400 * 86400

    ticket = _ok(client, "/api/tickets", headers=CUSTOMER)["tickets"][0]
    detail = _ok(client, f"/api/tickets/{ticket['id']}", headers=CUSTOMER)["ticket"]
    order = _ok(client, "/api/order", headers=CUSTOMER)["order"]
    scope = _ok(client, "/api/order/scope", headers=CUSTOMER)

    times = {
        "工单 created_at": ticket["created_at"],
        "订单 expires_at": order["expires_at"],
        "服务期 started_at": scope["period"]["started_at"],
        "服务期 expires_at": scope["period"]["expires_at"],
        "建单时间线 changed_at": detail["status_timeline"][0]["changed_at"],
    }
    for label, value in times.items():
        assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} 不是数值时间戳：{value!r}"
        assert abs(float(value) - now) <= year, f"{label} 偏离当前时间过远，可能单位不是秒：{value}"


# ---------------------------------------------------------------------------
# 四、入参越界
# ---------------------------------------------------------------------------


def test_入参越界统一_422_并记录响应体差异(client):
    """三个模块对越界入参都返回 422，但响应体形态目前分两类。

    工单与认证由业务异常给码（TICKET_ / AUTH_），订单走 FastAPI 的参数校验；
    两类都是 422，调用方按状态码分支即可。这条用例把差异显式钉住，
    后续若统一成同一种形态，改这里而不是让差异悄悄漂移。
    """
    ticket_page = client.get("/api/tickets?page=0", headers=ADMIN)
    ticket_size = client.get("/api/tickets?page_size=101", headers=ADMIN)
    order_page = client.get("/api/order/admin?page=0", headers=ADMIN)
    order_size = client.get("/api/order/admin?page_size=101", headers=ADMIN)
    auth_body = client.post("/api/login", json={"username": ""})

    assert ticket_page.status_code == 422 and ticket_size.status_code == 422
    assert order_page.status_code == 422 and order_size.status_code == 422
    assert auth_body.status_code == 422

    # 工单：业务错误体，带自己的错误码
    _assert_error_body(ticket_page.json(), "TICKET_", "/api/tickets?page=0")
    assert ticket_page.json()["error_code"] == TicketErrorCode.INVALID_PAGINATION
    assert ticket_size.json()["error_code"] == TicketErrorCode.INVALID_PAGINATION

    # 订单：框架校验结果，detail 为逐字段说明的列表
    body = order_page.json()
    assert "detail" in body and isinstance(body["detail"], list) and body["detail"]
    assert set(body) <= {"detail"}

    # 批量收敛条数：工单侧走业务错误体，区间与订单归档接口一致（1–500）
    for value in (0, 501):
        denied = client.post(f"/api/tickets/expire?limit={value}", headers=ADMIN)
        assert denied.status_code == 422, f"limit={value} 应被拒绝：{denied.text}"
        assert denied.json()["error_code"] == TicketErrorCode.INVALID_BATCH
    assert client.post("/api/tickets/expire?limit=500", headers=ADMIN).status_code == 200


def test_三模块路径前缀不交叉(client):
    """模块边界靠路径前缀表达，混入对方前缀会让权限与错误码无从归属。"""
    paths = set(client.get("/openapi.json").json()["paths"])
    auth_paths = {p for p in paths if p in {"/api/login", "/api/register", "/api/refresh",
                                           "/api/logout", "/api/me", "/api/sessions/cleanup"}}
    ticket_paths = {p for p in paths if p.startswith("/api/tickets")}
    order_paths = {p for p in paths if p.startswith("/api/order")}

    assert len(auth_paths) == 6, f"认证路由数量变化：{sorted(auth_paths)}"
    assert not (auth_paths & ticket_paths) and not (auth_paths & order_paths)
    assert not (ticket_paths & order_paths), f"路径前缀交叉：{ticket_paths & order_paths}"
    assert ticket_paths | order_paths | auth_paths <= paths
