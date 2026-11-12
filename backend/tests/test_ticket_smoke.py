"""工单模块全接口冒烟测试。

一次性走通模块对外暴露的每一个接口，验证三件事：
1. 路由可用：管理员 / 普通用户 / 匿名三种身份下，各接口都返回预期结果，
   且匿名与越权的响应结构一致，不会出现某个接口漏接身份校验；
2. 主流程连贯：建单 → 待确认 → 确认 → 列表 → 详情 → 回复 → 改状态 →
   规则判定 → 转人工，以及建单 → 取消、建单 → 超时收敛两条旁支；
3. 错误契约稳定：未登录、越权、工单不存在、入参非法都返回带 error_code 的
   统一错误体，调用方可以按错误码分支处理。

测试使用独立的临时 SQLite，不读写真实的 app.db。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api import tickets as api_tickets
from backend.database import Base
from backend.models import Order, Product, Session, Ticket, User
from backend.services import auth as auth_service
from backend.services import orders as orders_service
from backend.services import tickets

ADMIN = {"Authorization": "Bearer tk-smoke-admin"}
CUSTOMER = {"Authorization": "Bearer tk-smoke-customer"}

# 工单模块对外暴露的全部接口，冒烟时逐个覆盖。
ENDPOINTS = [
    ("get", "/api/tickets", {}),
    ("post", "/api/tickets", {"json": {"description": "导出失败"}}),
    ("get", "/api/tickets/pending", {}),
    ("get", "/api/tickets/stats", {}),
    ("get", "/api/tickets/T-0001", {}),
    ("get", "/api/tickets/T-0001/transfer-decision", {}),
    ("post", "/api/tickets/confirm", {"json": {"ticket_id": "T-0001"}}),
    ("post", "/api/tickets/cancel", {"json": {"ticket_id": "T-0001"}}),
    ("post", "/api/tickets/reply", {"json": {"ticket_id": "T-0001", "content": "已收到"}}),
    ("post", "/api/tickets/status", {"json": {"ticket_id": "T-0001", "status": "open"}}),
    ("post", "/api/tickets/transfer", {"json": {"ticket_id": "T-0001"}}),
    ("post", "/api/tickets/expire", {}),
]

# 订单查询接口对匿名是宽松的，只参与路由注册检查，不参与未登录拦截检查；
# 服务范围查询要求登录，但结论依赖具体的订单配置，同样只校验路由注册；
# 批量接口会真实改变工单状态，也只校验路由注册，不进冒烟主流程。
ROUTES = ENDPOINTS + [
    ("get", "/api/order", {}),
    ("get", "/api/order/list", {}),
    ("get", "/api/order/scope", {}),
    ("get", "/api/order/feature-scope", {}),
    ("get", "/api/order/renewal", {}),
    ("get", "/api/order/admin", {}),
    ("post", "/api/order/expire", {}),
    ("post", "/api/tickets/batch-close", {}),
    ("post", "/api/tickets/batch-transfer", {}),
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 预置管理员、客户、产品与订单，挂载工单路由。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'smoke.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, tickets, auth_service, orders_service):
        monkeypatch.setattr(module, "SessionLocal", factory)

    now = time.time()
    db = factory()
    try:
        db.add_all(
            [
                User(id="u-admin", username="smoke-admin", password_hash="x", name="客服小张", role="admin"),
                User(id="u-cust", username="smoke-cust", password_hash="x", name="客户小李", role="customer"),
                Session(token="tk-smoke-admin", user_id="u-admin", username="smoke-admin",
                        name="客服小张", role="admin", expires_at=now + 3600),
                Session(token="tk-smoke-customer", user_id="u-cust", username="smoke-cust",
                        name="客户小李", role="customer", expires_at=now + 3600),
                Product(id="pro", name="专业版", features=["导出", "批量处理"]),
                Order(id="o-smoke", user_id="u-cust", product_tier="pro", status="active",
                      started_at=now - 86400, expires_at=now + 86400 * 30),
            ]
        )
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.include_router(api_tickets.tickets_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _create(client, description: str, headers=CUSTOMER) -> dict:
    response = client.post(
        "/api/tickets",
        json={"description": description, "tried_steps": "重启客户端", "evidence": "日志无异常"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def _age_ticket(session_factory, ticket_id: str, seconds: int) -> None:
    """把草稿的建单时间前移，模拟放置过久的待确认工单。"""
    db = session_factory()
    try:
        db.query(Ticket).filter(Ticket.id == ticket_id).update({"created_at": time.time() - seconds})
        db.commit()
    finally:
        db.close()


def test_全部接口在匿名访问下返回统一的未登录响应(client):
    """除订单查询外，所有工单接口都要求登录，且错误体结构一致。"""
    for method, path, kwargs in ENDPOINTS:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, f"{method.upper()} {path} 未拒绝匿名访问"
        payload = response.json()
        assert payload["error_code"] == "TICKET_UNAUTHORIZED"
        assert payload["error"] == "未登录"
        assert "error_detail" not in payload

    # 订单查询对匿名是宽松的：返回空订单而不是报错，前端可据此引导登录
    assert client.get("/api/order").json() == {"order": None}


def test_普通用户调用管理接口被拒绝且错误码一致(client):
    """回复、改状态、转人工、超时收敛四个管理动作对普通用户关闭。"""
    for method, path, kwargs in (
        ("post", "/api/tickets/reply", {"json": {"ticket_id": "T-0001", "content": "已收到"}}),
        ("post", "/api/tickets/status", {"json": {"ticket_id": "T-0001", "status": "resolved"}}),
        ("post", "/api/tickets/transfer", {"json": {"ticket_id": "T-0001"}}),
        ("post", "/api/tickets/expire", {}),
    ):
        response = getattr(client, method)(path, headers=CUSTOMER, **kwargs)
        assert response.status_code == 403, f"{path} 未拒绝普通用户"
        assert response.json() == {"error": "无权限", "error_code": "TICKET_FORBIDDEN"}


def test_主流程串起建单确认回复改状态判定转人工(client):
    """客户建单、管理员处理的主链路，逐接口断言关键响应字段。"""
    created = _create(client, "专业版导出功能报错")
    assert created["created"] is True
    ticket_id = created["ticket"]["id"]
    assert created["ticket"]["status"] == "pending_confirm"

    pending = client.get("/api/tickets/pending", headers=CUSTOMER).json()
    assert pending["total"] == 1
    assert pending["tickets"][0]["id"] == ticket_id
    assert pending["tickets"][0]["expired"] is False

    confirmed = client.post(
        "/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=CUSTOMER
    ).json()
    assert confirmed["ticket"]["status"] == "open"

    listed = client.get("/api/tickets", params={"status": "open"}, headers=CUSTOMER).json()
    assert [item["id"] for item in listed["tickets"]] == [ticket_id]
    assert listed["statuses"] == ["open"]

    order = client.get("/api/order", headers=CUSTOMER).json()["order"]
    assert order["has_order"] is True and order["product_name"] == "专业版"

    replied = client.post(
        "/api/tickets/reply",
        json={"ticket_id": ticket_id, "content": "已定位到权限配置问题"},
        headers=ADMIN,
    ).json()
    assert replied["ticket"]["status"] == "in_progress"

    # 功能在专业版能力内、排查轮次未达阈值，规则判定为无需转人工
    in_scope = client.get(
        f"/api/tickets/{ticket_id}/transfer-decision",
        params={"feature": "导出"},
        headers=ADMIN,
    ).json()
    assert in_scope["decision"]["should_transfer"] is False
    assert in_scope["decision"]["basis"]["in_service"] is True

    # 用户主动要求转人工，规则命中后按规则执行转人工
    decision = client.get(
        f"/api/tickets/{ticket_id}/transfer-decision",
        params={"user_text": "麻烦帮我转人工处理"},
        headers=ADMIN,
    ).json()["decision"]
    assert decision["should_transfer"] is True
    assert decision["reason_codes"] == ["user_requested"]

    transferred = client.post(
        "/api/tickets/transfer",
        json={"ticket_id": ticket_id, "user_text": "麻烦帮我转人工处理", "auto": True},
        headers=ADMIN,
    ).json()
    assert transferred["ticket"]["status"] == "transferred_human"
    assert transferred["ticket"]["transfer"]["by_rule"] is True
    assert transferred["ticket"]["transfer"]["reason_codes"] == ["user_requested"]

    detail = client.get(f"/api/tickets/{ticket_id}", headers=CUSTOMER).json()
    ticket = detail["ticket"]
    assert [item["to_status"] for item in ticket["status_timeline"]] == [
        "pending_confirm",
        "open",
        "in_progress",
        "transferred_human",
    ]
    assert ticket["reply_timeline"]["human_count"] == 1
    assert ticket["reply_timeline"]["has_replies"] is True
    assert ticket["detail_summary"]["status_chain"] == [
        "pending_confirm",
        "open",
        "in_progress",
        "transferred_human",
    ]
    assert ticket["detail_summary"]["status_change_count"] == 4
    assert ticket["detail_summary"]["transfer_count"] == 1
    assert ticket["troubleshooting"]["tried_step_count"] == 1
    assert detail["message"].startswith(f"工单 {ticket_id}")

    # 转人工后由管理员推进到已解决，再关闭，状态机全程可流转
    resolved = client.post(
        "/api/tickets/status",
        json={"ticket_id": ticket_id, "status": "resolved"},
        headers=ADMIN,
    ).json()
    assert resolved["ticket"]["status"] == "resolved"
    closed = client.post(
        "/api/tickets/status",
        json={"ticket_id": ticket_id, "status": "closed"},
        headers=ADMIN,
    ).json()
    assert closed["ticket"]["status"] == "closed"

    # 已关闭工单不再参与待确认列表，规则判定也给出「无需判定」
    assert client.get("/api/tickets/pending", headers=CUSTOMER).json()["total"] == 0
    final = client.get(
        f"/api/tickets/{ticket_id}/transfer-decision", headers=ADMIN
    ).json()["decision"]
    assert final["applicable"] is False and final["should_transfer"] is False


def test_取消与超时收敛两条旁支接口(client):
    """草稿可直接取消，超时草稿可由管理员批量收敛为已关闭。"""
    canceled = _create(client, "误报的导出问题")["ticket"]["id"]
    result = client.post(
        "/api/tickets/cancel",
        json={"ticket_id": canceled, "reason": "问题已自行解决"},
        headers=CUSTOMER,
    ).json()
    assert result["ticket"]["status"] == "closed"

    detail = client.get(f"/api/tickets/{canceled}", headers=CUSTOMER).json()["ticket"]
    assert [item["to_status"] for item in detail["status_timeline"]] == ["pending_confirm", "closed"]
    assert any("问题已自行解决" in reply["content"] for reply in detail["replies"])

    expired = _create(client, "长时间未确认的草稿")["ticket"]["id"]
    stale = _create(client, "另一个超时草稿")["ticket"]["id"]
    factory = tickets.SessionLocal
    _age_ticket(factory, expired, 7200)
    _age_ticket(factory, stale, 7200)

    swept = client.post(
        "/api/tickets/expire", params={"timeout_seconds": 1800, "limit": 1}, headers=ADMIN
    ).json()
    assert swept["result"]["closed"] == 1
    assert swept["result"]["remaining"] == 1

    swept_again = client.post(
        "/api/tickets/expire", params={"timeout_seconds": 1800}, headers=ADMIN
    ).json()
    assert swept_again["result"]["closed"] == 1
    assert swept_again["result"]["remaining"] == 0

    statuses = {
        client.get(f"/api/tickets/{tid}", headers=CUSTOMER).json()["ticket"]["status"]
        for tid in (expired, stale)
    }
    assert statuses == {"closed"}


def test_错误响应携带稳定错误码(client):
    """不存在、未知状态、非法分页与筛选都返回统一错误体，便于前端分支处理。"""
    missing = client.get("/api/tickets/T-9999", headers=CUSTOMER)
    assert missing.status_code == 404
    assert missing.json() == {
        "error": "工单不存在",
        "error_code": "TICKET_NOT_FOUND",
        "error_detail": {"ticket_id": "T-9999"},
    }

    ticket_id = _create(client, "状态机冒烟工单")["ticket"]["id"]
    confirmed = client.post(
        "/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=CUSTOMER
    ).json()
    assert confirmed["ticket"]["status"] == "open"

    unknown = client.post(
        "/api/tickets/status",
        json={"ticket_id": ticket_id, "status": "archived"},
        headers=ADMIN,
    )
    assert unknown.status_code == 422
    assert unknown.json()["error_code"] == "TICKET_UNKNOWN_STATUS"
    assert unknown.json()["error_detail"] == {"from_status": "open", "to_status": "archived"}

    illegal = client.post(
        "/api/tickets/status",
        json={"ticket_id": ticket_id, "status": "pending_confirm"},
        headers=ADMIN,
    )
    assert illegal.status_code == 409
    assert illegal.json()["error_code"] == "TICKET_INVALID_TRANSITION"

    for params, code in (
        ({"page": 0}, "TICKET_INVALID_PAGINATION"),
        ({"page_size": 1000}, "TICKET_INVALID_PAGINATION"),
        ({"status": "archived"}, "TICKET_INVALID_STATUS_FILTER"),
        ({"order": "sideways"}, "TICKET_INVALID_ORDER"),
        ({"keyword": "长" * 60}, "TICKET_INVALID_KEYWORD"),
    ):
        response = client.get("/api/tickets", params=params, headers=CUSTOMER)
        assert response.status_code == 422, f"{params} 未按非法入参拒绝"
        assert response.json()["error_code"] == code

    # 请求模型层的校验仍由 FastAPI 返回 detail 列表，与业务错误体区分开
    blank = client.post("/api/tickets", json={"description": "   "}, headers=CUSTOMER)
    assert blank.status_code == 422
    assert isinstance(blank.json()["detail"], list)


def test_全部接口已注册到路由表(client):
    """路由表覆盖全部对外接口，冒烟清单与实现不会悄悄脱节。"""
    paths = client.app.openapi()["paths"]
    expected = {path.replace("T-0001", "{ticket_id}") for _, path, _ in ROUTES}
    assert set(paths) == expected
    assert set(paths["/api/tickets"]) == {"get", "post"}
    assert set(paths["/api/tickets/{ticket_id}"]) == {"get"}
    assert set(paths["/api/tickets/{ticket_id}/transfer-decision"]) == {"get"}
