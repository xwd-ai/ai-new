"""认证身份与工单写入联动测试。

覆盖三件事：
1. 身份换算：登录用户如何变成工单侧的操作人（归属号、展示名、角色），
   以及身份缺失时是否被明确拒绝而不是写成空归属；
2. 身份落痕：建单、确认、取消三个写动作是否把真实操作人记进状态时间线，
   系统动作是否仍落到兜底名称；
3. 身份贯通：接口层用真实 JWT 访问时，工单归属是否只认令牌身份，
   请求体里的其它客户编号不会改变归属。

测试使用独立的临时 SQLite，不读写真实 app.db。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import tickets as api_tickets
from backend.database import Base
from backend.models import Session, Ticket, User
from backend.models.ticket import TicketAuthRequiredError
from backend.services import auth as auth_service
from backend.tests.reference_api import ticket_service as tickets

ADMIN_ID = "u-id-admin"
CUSTOMER_ID = "u-id-cust"
OTHER_ID = "u-id-other"


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """临时库：预置管理员、两个客户账号，并替换各模块的 SessionLocal。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'identity.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, tickets, auth_service):
        monkeypatch.setattr(module, "SessionLocal", factory)

    now = time.time()
    db = factory()
    try:
        db.add_all(
            [
                User(id=ADMIN_ID, username="id-admin", password_hash="x", name="客服小张", role="admin"),
                User(id=CUSTOMER_ID, username="id-cust", password_hash="x", name="客户小李", role="customer"),
                User(id=OTHER_ID, username="id-other", password_hash="x", name="客户小王", role="customer"),
                Session(token="tk-id-cust", user_id=CUSTOMER_ID, username="id-cust",
                        name="客户小李", role="customer", expires_at=now + 3600),
            ]
        )
        db.commit()
    finally:
        db.close()

    yield factory
    engine.dispose()


def _user(user_id: str, name: str = "", role: str = "customer", username: str = "") -> dict:
    """构造一份登录用户字典，字段与认证层 _account 的输出一致。"""
    return {
        "id": user_id,
        "username": username or f"name-of-{user_id}",
        "name": name,
        "role": role,
    }


def _token(user: dict) -> dict:
    """为任意用户签一张真实访问令牌，返回可用的请求头。"""
    return {"Authorization": f"Bearer {auth_service.create_access_token(user)}"}


def _timeline(ticket_id: str) -> list[dict]:
    """取工单的状态时间线。"""
    return tickets.get_ticket(ticket_id)["status_timeline"]


# --------------------------------------------------------------------------
# 身份换算
# --------------------------------------------------------------------------


def test_身份换算保留归属号展示名与角色(db_env):
    identity = tickets.ticket_identity(_user(CUSTOMER_ID, "客户小李"))
    assert identity["id"] == CUSTOMER_ID
    assert identity["name"] == "客户小李"
    assert identity["role"] == "customer"
    assert identity["is_admin"] is False


def test_管理员身份换算标记为管理员(db_env):
    identity = tickets.ticket_identity(_user(ADMIN_ID, "客服小张", role="admin"))
    assert identity["is_admin"] is True
    assert identity["role"] == "admin"


def test_昵称为空时回退用户名(db_env):
    identity = tickets.ticket_identity(_user(CUSTOMER_ID, "", username="id-cust"))
    assert identity["name"] == "id-cust"


def test_昵称与用户名都为空时回退归属号(db_env):
    user = {"id": CUSTOMER_ID, "role": "customer"}
    assert tickets.ticket_identity(user)["name"] == CUSTOMER_ID


def test_昵称压空白并截断超长(db_env):
    identity = tickets.ticket_identity(_user(CUSTOMER_ID, "  客户 \n 小李  " + "很" * 40))
    assert identity["name"].startswith("客户 小李")
    assert len(identity["name"]) == tickets.OPERATOR_NAME_MAX_LENGTH


def test_未登录换算身份直接拒绝(db_env):
    with pytest.raises(TicketAuthRequiredError) as excinfo:
        tickets.ticket_identity(None)
    assert excinfo.value.code == "TICKET_UNAUTHORIZED"
    assert excinfo.value.http_status == 401


def test_空字典与缺少归属号同样拒绝(db_env):
    for user in ({}, {"id": "  "}, {"name": "只有名字"}):
        with pytest.raises(TicketAuthRequiredError):
            tickets.ticket_identity(user)


def test_身份换算不修改入参(db_env):
    user = _user(CUSTOMER_ID, "客户小李")
    snapshot = dict(user)
    tickets.ticket_identity(user)
    assert user == snapshot


def test_已换算的身份块可再次换算(db_env):
    once = tickets.ticket_identity(_user(CUSTOMER_ID, "客户小李"))
    twice = tickets.ticket_identity(once)
    assert twice == once


# --------------------------------------------------------------------------
# 身份落痕：建单 / 确认 / 取消
# --------------------------------------------------------------------------


def test_建单时间线记录真实建单人(db_env):
    ticket, created, identity = tickets.create_ticket_for_user(
        _user(CUSTOMER_ID, "客户小李"), description="专业版导出失败"
    )
    assert created is True
    assert identity["id"] == CUSTOMER_ID
    assert ticket["user_id"] == CUSTOMER_ID
    assert ticket["user_name"] == "客户小李"
    first = _timeline(ticket["id"])[0]
    assert first["from_status"] == ""
    assert first["to_status"] == "pending_confirm"
    assert first["operator"] == "客户小李"


def test_建单不传操作人时落到兜底名称(db_env):
    ticket, _ = tickets.create_ticket(CUSTOMER_ID, "客户小李", "导出失败", "", "")
    assert _timeline(ticket["id"])[0]["operator"] == tickets.DEFAULT_OPERATOR


def test_建单传空白操作人同样落到兜底名称(db_env):
    ticket, _ = tickets.create_ticket(
        CUSTOMER_ID, "客户小李", "批量导入失败", "", "", operator="   "
    )
    assert _timeline(ticket["id"])[0]["operator"] == tickets.DEFAULT_OPERATOR


def test_重复建单命中已有工单时身份保持一致(db_env):
    user = _user(CUSTOMER_ID, "客户小李")
    first, created_first, identity_first = tickets.create_ticket_for_user(user, description="同题")
    second, created_second, identity_second = tickets.create_ticket_for_user(user, description="同题")
    assert created_first is True and created_second is False
    assert second["id"] == first["id"]
    assert identity_second == identity_first


def test_确认时间线记录确认人(db_env):
    user = _user(CUSTOMER_ID, "客户小李")
    ticket, _, _ = tickets.create_ticket_for_user(user, description="确认人落痕")
    tickets.confirm_ticket(ticket["id"], operator="客户小李")
    timeline = _timeline(ticket["id"])
    assert timeline[-1]["to_status"] == "open"
    assert timeline[-1]["operator"] == "客户小李"


def test_管理员代确认时记录管理员(db_env):
    customer = _user(CUSTOMER_ID, "客户小李")
    admin = tickets.ticket_identity(_user(ADMIN_ID, "客服小张", role="admin"))
    ticket, _, _ = tickets.create_ticket_for_user(customer, description="代确认落痕")
    tickets.confirm_ticket(ticket["id"], operator=admin["name"])
    timeline = _timeline(ticket["id"])
    assert timeline[0]["operator"] == "客户小李"
    assert timeline[-1]["operator"] == "客服小张"


def test_取消时间线记录取消人且说明仍归系统(db_env):
    user = _user(CUSTOMER_ID, "客户小李")
    ticket, _, _ = tickets.create_ticket_for_user(user, description="取消人落痕")
    tickets.cancel_ticket(ticket["id"], "问题已自行解决", operator="客户小李")
    detail = tickets.get_ticket(ticket["id"])
    assert detail["status"] == "closed"
    assert detail["status_timeline"][-1]["operator"] == "客户小李"
    assert detail["replies"][-1]["replier"] == "system"
    assert detail["reply_timeline"]["human_count"] == 0


def test_未登录建单被拒绝且不产生工单(db_env):
    with pytest.raises(TicketAuthRequiredError):
        tickets.create_ticket_for_user(None, description="匿名建单")
    db = db_env()
    try:
        assert db.query(Ticket).count() == 0
    finally:
        db.close()


# --------------------------------------------------------------------------
# 身份贯通：接口层
# --------------------------------------------------------------------------


@pytest.fixture()
def client(db_env):
    """只挂工单路由的临时应用，身份全部由真实 JWT 解析。"""
    app = FastAPI()
    app.include_router(api_tickets.tickets_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


def test_匿名建单返回统一未登录错误(client):
    response = client.post("/api/tickets", json={"description": "导出失败"})
    assert response.status_code == 401
    body = response.json()
    assert body["error_code"] == "TICKET_UNAUTHORIZED"


def test_令牌身份决定建单归属(client):
    headers = _token(_user(CUSTOMER_ID, "客户小李"))
    response = client.post(
        "/api/tickets",
        json={"description": "专业版导出失败", "tried_steps": "重启客户端"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["ticket"]["user_id"] == CUSTOMER_ID
    assert body["ticket"]["user_name"] == "客户小李"
    assert body["identity"] == {
        "id": CUSTOMER_ID,
        "name": "客户小李",
        "role": "customer",
        "is_admin": False,
    }
    assert _timeline(body["ticket"]["id"])[0]["operator"] == "客户小李"


def test_请求体塞入他人客户编号不改变归属(client):
    headers = _token(_user(CUSTOMER_ID, "客户小李"))
    response = client.post(
        "/api/tickets",
        json={"description": "越权归属尝试", "user_id": OTHER_ID, "user_name": "客户小王"},
        headers=headers,
    )
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert ticket["user_id"] == CUSTOMER_ID
    assert ticket["user_name"] == "客户小李"


def test_两个账号各自建单互不串号(client):
    headers_a = _token(_user(CUSTOMER_ID, "客户小李"))
    headers_b = _token(_user(OTHER_ID, "客户小王"))
    first = client.post("/api/tickets", json={"description": "同一句描述"}, headers=headers_a)
    second = client.post("/api/tickets", json={"description": "同一句描述"}, headers=headers_b)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["ticket"]["id"] != second.json()["ticket"]["id"]
    assert second.json()["identity"]["id"] == OTHER_ID


def test_确认接口按令牌身份落痕(client):
    headers = _token(_user(CUSTOMER_ID, "客户小李"))
    created = client.post("/api/tickets", json={"description": "确认落痕"}, headers=headers)
    ticket_id = created.json()["ticket"]["id"]

    response = client.post("/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ticket"]["status"] == "open"
    assert body["identity"]["id"] == CUSTOMER_ID
    assert _timeline(ticket_id)[-1]["operator"] == "客户小李"


def test_管理员确认他人工单记管理员名(client):
    customer_headers = _token(_user(CUSTOMER_ID, "客户小李"))
    admin_headers = _token(_user(ADMIN_ID, "客服小张", role="admin"))
    created = client.post("/api/tickets", json={"description": "代确认"}, headers=customer_headers)
    ticket_id = created.json()["ticket"]["id"]

    response = client.post("/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=admin_headers)
    assert response.status_code == 200
    assert _timeline(ticket_id)[-1]["operator"] == "客服小张"


def test_取消接口按令牌身份落痕(client):
    headers = _token(_user(CUSTOMER_ID, "客户小李"))
    created = client.post("/api/tickets", json={"description": "取消落痕"}, headers=headers)
    ticket_id = created.json()["ticket"]["id"]

    response = client.post(
        "/api/tickets/cancel",
        json={"ticket_id": ticket_id, "reason": "已自行解决"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ticket"]["status"] == "closed"
    assert _timeline(ticket_id)[-1]["operator"] == "客户小李"


def test_过期令牌建单按未登录拒绝(client):
    """签名合格但已过期的令牌不构成登录身份，建单回到未登录语义。"""
    expired = _user(CUSTOMER_ID, "客户小李")
    past = int(time.time()) - 3600
    payload = auth_service.build_access_payload(expired)
    payload.update({"iat": past, "nbf": past, "exp": past + 30})
    headers = {"Authorization": f"Bearer {auth_service.sign_jwt(payload)}"}

    response = client.post("/api/tickets", json={"description": "过期令牌"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["error_code"] == "TICKET_UNAUTHORIZED"
