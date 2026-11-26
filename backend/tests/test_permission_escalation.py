"""权限越权测试：客户账号触碰管理接口与他人数据必须被拦。

测试盯的是「身份闸门」本身，而不是各接口的业务分支，因此刻意绕开正常流程，
从三个方向去撞：

1. 管理接口：会话清理、回复、改状态、转人工、批量关闭、批量转人工、超时收敛，
   匿名一律 401、客户一律 403，且权限判定发生在入参校验与资源查找之前——
   带着非法请求体或不存在工单号来撞，也只会拿到 401/403，拿不到 422/404；
2. 数据归属：详情、转人工判定、确认、取消按工单归属判断，客户访问他人工单 403；
   列表、待确认、统计只回本人数据，显式要求全量或指定他人客户号都拿不到别人的单；
3. 提权尝试：令牌里写死 role=admin、会话登记里存 admin 快照，都不能把客户变成管理员，
   角色一律回库判定；反过来，库里的角色改了之后同一张令牌立刻按新角色生效。

错误体沿用各模块既有契约：工单接口 TICKET_UNAUTHORIZED / TICKET_FORBIDDEN，
认证接口 AUTH_TOKEN_MISSING / AUTH_FORBIDDEN。全部用例使用独立临时 SQLite。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import tickets as api_tickets
from backend.tests.reference_api.auth import auth_router
from backend.database import Base
from backend.models import Order, Product, Session, User
from backend.models.ticket import TicketErrorCode, TicketStatus
from backend.services import auth as auth_service
from backend.services import orders as orders_service
from backend.tests.reference_api import ticket_service as ticket_service

ADMIN_TOKEN = "tk-perm-admin"
CUSTOMER_TOKEN = "tk-perm-cust"
OTHER_TOKEN = "tk-perm-other"

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
CUSTOMER = {"Authorization": f"Bearer {CUSTOMER_TOKEN}"}
OTHER = {"Authorization": f"Bearer {OTHER_TOKEN}"}

ADMIN_ID = "u-perm-admin"
CUSTOMER_ID = "u-perm-cust"
OTHER_ID = "u-perm-other"

# 不存在的工单号：用于把「权限判定」和「资源查找」两步分开观察——
# 匿名 401、客户 403、管理员走到查找才 404，三步顺序一目了然。
MISSING_ID = "T-999999"

# 管理接口清单：路径 / 请求体 / 管理员调用后的预期状态。
# 批量接口对不存在的工单不报错（逐单记入 failures），因此管理员的预期是 200。
TICKET_ADMIN_ENDPOINTS = [
    ("/api/tickets/reply", {"ticket_id": MISSING_ID, "content": "正在排查"}, 404),
    ("/api/tickets/status", {"ticket_id": MISSING_ID, "status": "resolved"}, 404),
    ("/api/tickets/transfer", {"ticket_id": MISSING_ID, "reason": "需要人工介入"}, 404),
    ("/api/tickets/batch-close", {"ticket_ids": [MISSING_ID], "reason": "批量关闭"}, 200),
    ("/api/tickets/batch-transfer", {"ticket_ids": [MISSING_ID], "reason": "批量转人工"}, 200),
    ("/api/tickets/expire", {}, 200),
]


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    """固定签名密钥，避免提权用例受本机环境变量影响。"""
    monkeypatch.setenv(auth_service.JWT_SECRET_ENV, "unit-test-permission-secret-0123456789")
    monkeypatch.delenv(auth_service.JWT_LEEWAY_ENV, raising=False)


def _seed(session_factory):
    """一个管理员、两个客户，各自有一条会话与一张订单。"""
    now = time.time()
    db = session_factory()
    try:
        db.add_all(
            [
                User(id=ADMIN_ID, username="perm-admin", password_hash="x", name="客服小张", role="admin"),
                User(id=CUSTOMER_ID, username="perm-cust", password_hash="x", name="客户小李", role="customer"),
                User(id=OTHER_ID, username="perm-other", password_hash="x", name="客户老王", role="customer"),
                Session(token=ADMIN_TOKEN, user_id=ADMIN_ID, username="perm-admin",
                        name="客服小张", role="admin", expires_at=now + 3600),
                Session(token=CUSTOMER_TOKEN, user_id=CUSTOMER_ID, username="perm-cust",
                        name="客户小李", role="customer", expires_at=now + 3600),
                Session(token=OTHER_TOKEN, user_id=OTHER_ID, username="perm-other",
                        name="客户老王", role="customer", expires_at=now + 3600),
                Product(id="perm-pro", name="专业版", features=["导出", "批量处理"]),
                Order(id="o-perm-cust", user_id=CUSTOMER_ID, product_tier="pro", status="active",
                      started_at=now - 86400, expires_at=now + 86400 * 30),
                Order(id="o-perm-other", user_id=OTHER_ID, product_tier="pro", status="active",
                      started_at=now - 86400, expires_at=now + 86400 * 30),
            ]
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 三个账号；服务层的 SessionLocal 一并指向它。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'permission.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, ticket_service, auth_service, orders_service):
        monkeypatch.setattr(module, "SessionLocal", session_factory)
    _seed(session_factory)
    yield session_factory
    engine.dispose()


@pytest.fixture()
def client(factory):
    """认证 + 工单两组路由，正是越权测试需要的那部分对外接口。"""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(api_tickets.tickets_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


def _create(client, description: str, headers=CUSTOMER) -> str:
    """建单并返回工单号，草稿固定落在待确认状态。"""
    response = client.post("/api/tickets", json={"description": description}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["ticket"]["id"]


def _confirm(client, ticket_id: str, headers=CUSTOMER) -> str:
    """确认草稿，返回工单号，后续写操作需要 open 及以上状态。"""
    response = client.post("/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=headers)
    assert response.status_code == 200, response.text
    return ticket_id


# ---------------------------------------------------------------------------
# 身份闸门：匿名 401、客户 403，管理员才被放行
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,payload,admin_status", TICKET_ADMIN_ENDPOINTS)
def test_ticket_admin_endpoints_reject_non_admin(client, path, payload, admin_status):
    """客户与匿名请求管理接口一律被拦，管理员才走到业务层。"""
    anonymous = client.post(path, json=payload)
    assert anonymous.status_code == 401
    assert anonymous.json()["error_code"] == TicketErrorCode.UNAUTHORIZED

    customer = client.post(path, json=payload, headers=CUSTOMER)
    assert customer.status_code == 403
    assert customer.json()["error_code"] == TicketErrorCode.FORBIDDEN

    # 管理员通过闸门后才会遇到「工单不存在」或「无事可做」，不会停在 401/403
    admin = client.post(path, json=payload, headers=ADMIN)
    assert admin.status_code == admin_status
    assert "error_code" not in admin.json() or admin.json()["error_code"] not in (
        TicketErrorCode.UNAUTHORIZED,
        TicketErrorCode.FORBIDDEN,
    )


def test_cleanup_endpoint_requires_admin(client):
    """会话清理是运维动作：匿名 401、客户 403、管理员 200。"""
    anonymous = client.post("/api/sessions/cleanup", json={})
    assert anonymous.status_code == 401
    assert anonymous.json()["error_code"] == "AUTH_TOKEN_MISSING"

    customer = client.post("/api/sessions/cleanup", json={}, headers=CUSTOMER)
    assert customer.status_code == 403
    body = customer.json()
    assert body["error_code"] == "AUTH_FORBIDDEN"
    assert body["error_detail"]["required_roles"] == [auth_service.ADMIN_ROLE]
    assert body["error_detail"]["role"] == auth_service.CUSTOMER_ROLE

    admin = client.post("/api/sessions/cleanup", json={}, headers=ADMIN)
    assert admin.status_code == 200
    assert set(admin.json()) >= {"deleted", "total", "cutoff", "truncated"}


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/tickets/reply", {"ticket_id": "", "content": ""}),
        ("/api/tickets/status", {}),
        ("/api/tickets/batch-close", {"ticket_ids": []}),
        ("/api/sessions/cleanup", {"retention": -1}),
    ],
)
def test_permission_is_checked_before_payload_validation(client, path, payload):
    """请求体再离谱也先撞权限：非法入参不会把 401/403 变成 422。"""
    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, json=payload, headers=CUSTOMER).status_code == 403

    # 管理员同一份非法请求体才会被入参校验拒绝，说明上面的 403 来自权限而非格式
    admin = client.post(path, json=payload, headers=ADMIN)
    assert admin.status_code in (400, 404, 409, 422)


# ---------------------------------------------------------------------------
# 管理员正常放行：闸门不能把该放行的也拦掉
# ---------------------------------------------------------------------------


def test_admin_can_operate_on_customer_tickets(client):
    """管理员对客户工单的写操作全部放行，并真实生效。"""
    replied = _confirm(client, _create(client, "专业版导出失败"))
    response = client.post(
        "/api/tickets/reply",
        json={"ticket_id": replied, "content": "已转研发确认导出权限"},
        headers=ADMIN,
    )
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert ticket["status"] == TicketStatus.IN_PROGRESS.value
    assert any(item["content"] == "已转研发确认导出权限" for item in ticket["replies"])

    resolved = client.post(
        "/api/tickets/status", json={"ticket_id": replied, "status": "resolved"}, headers=ADMIN
    )
    assert resolved.status_code == 200
    assert resolved.json()["ticket"]["status"] == TicketStatus.RESOLVED.value

    closed = _confirm(client, _create(client, "账号无法登录"))
    batch_close = client.post(
        "/api/tickets/batch-close",
        json={"ticket_ids": [closed], "reason": "重复提问合并处理"},
        headers=ADMIN,
    )
    assert batch_close.status_code == 200
    assert batch_close.json()["result"]["succeeded"] == 1

    transferred = _confirm(client, _create(client, "导出后文件损坏"))
    batch_transfer = client.post(
        "/api/tickets/batch-transfer",
        json={"ticket_ids": [transferred], "reason": "需要人工复现"},
        headers=ADMIN,
    )
    assert batch_transfer.status_code == 200
    assert batch_transfer.json()["result"]["succeeded"] == 1

    expired = client.post("/api/tickets/expire", headers=ADMIN)
    assert expired.status_code == 200
    assert "result" in expired.json()


# ---------------------------------------------------------------------------
# 数据归属：客户的单归客户，管理员才跨账号
# ---------------------------------------------------------------------------


def test_customer_cannot_operate_other_customer_ticket(client):
    """他人工单的查看、判定、确认、取消全部 403，且工单状态不被改动。"""
    other_ticket = _create(client, "他人订单无法导出", headers=OTHER)

    assert client.get(f"/api/tickets/{other_ticket}", headers=CUSTOMER).status_code == 403
    assert client.get(
        f"/api/tickets/{other_ticket}/transfer-decision", headers=CUSTOMER
    ).status_code == 403
    confirm = client.post("/api/tickets/confirm", json={"ticket_id": other_ticket}, headers=CUSTOMER)
    assert confirm.status_code == 403
    assert confirm.json()["error_code"] == TicketErrorCode.FORBIDDEN
    cancel = client.post("/api/tickets/cancel", json={"ticket_id": other_ticket}, headers=CUSTOMER)
    assert cancel.status_code == 403
    assert cancel.json()["error_detail"]["owner_id"] == OTHER_ID

    detail = client.get(f"/api/tickets/{other_ticket}", headers=ADMIN).json()["ticket"]
    assert detail["status"] == TicketStatus.PENDING_CONFIRM.value
    assert detail["replies"] == []

    # 工单本人与管理员则不受影响，越权拦截没有误伤正常路径
    assert client.get(f"/api/tickets/{other_ticket}", headers=OTHER).status_code == 200
    owned = _create(client, "本人导出失败")
    assert client.post(
        "/api/tickets/confirm", json={"ticket_id": owned}, headers=CUSTOMER
    ).status_code == 200


def test_customer_list_pending_and_stats_are_scoped(client):
    """列表、待确认、统计只统计本人，显式要全量或指定他人客户号都无效。"""
    _create(client, "本人专业版导出失败")
    _create(client, "他人基础版导出失败", headers=OTHER)
    _create(client, "他人账号被锁定", headers=OTHER)

    listed = client.get("/api/tickets", headers=CUSTOMER).json()
    assert listed["total"] == 1
    assert {item["user_id"] for item in listed["tickets"]} == {CUSTOMER_ID}

    pending = client.get("/api/tickets/pending", params={"all_users": True}, headers=CUSTOMER).json()
    assert pending["total"] == 1

    stats = client.get("/api/tickets/stats", headers=CUSTOMER).json()["stats"]
    assert stats["total"] == 1
    assert stats["user_id"] == CUSTOMER_ID

    foreign = client.get(
        "/api/tickets/stats", params={"user_id": OTHER_ID}, headers=CUSTOMER
    )
    assert foreign.status_code == 403
    assert foreign.json()["error_code"] == TicketErrorCode.FORBIDDEN

    admin_list = client.get("/api/tickets", headers=ADMIN).json()
    assert admin_list["total"] == 3
    admin_stats = client.get(
        "/api/tickets/stats", params={"user_id": OTHER_ID}, headers=ADMIN
    )
    assert admin_stats.status_code == 200
    assert admin_stats.json()["stats"]["total"] == 2


# ---------------------------------------------------------------------------
# 提权尝试：令牌与会话里的角色快照都不作数
# ---------------------------------------------------------------------------


def test_forged_role_claim_in_token_does_not_grant_admin(client, factory):
    """令牌声明 role=admin 但库里是客户，仍按客户处理；改库后同一令牌随新角色生效。"""
    forged = auth_service.create_access_token(
        {"id": CUSTOMER_ID, "username": "perm-cust", "role": auth_service.ADMIN_ROLE}
    )
    headers = {"Authorization": f"Bearer {forged}"}

    denied = client.post("/api/sessions/cleanup", json={}, headers=headers)
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "AUTH_FORBIDDEN"
    assert denied.json()["error_detail"]["role"] == auth_service.CUSTOMER_ROLE

    ticket_id = _create(client, "本人订单无法续费")
    assert client.post(
        "/api/tickets/reply", json={"ticket_id": ticket_id, "content": "处理中"}, headers=headers
    ).status_code == 403

    # 角色判定回库：改写库里的角色后，同一张令牌立刻按新角色放行
    db = factory()
    try:
        account = db.query(User).filter(User.id == CUSTOMER_ID).first()
        account.role = auth_service.ADMIN_ROLE
        db.commit()
    finally:
        db.close()

    assert client.post("/api/sessions/cleanup", json={}, headers=headers).status_code == 200


def test_session_role_snapshot_cannot_escalate(client, factory):
    """会话表里存着 admin 快照也没用：身份以用户表为准。"""
    now = time.time()
    db = factory()
    try:
        db.add(
            Session(token="tk-perm-tampered", user_id=CUSTOMER_ID, username="perm-cust",
                    name="客户小李", role="admin", expires_at=now + 3600)
        )
        db.commit()
    finally:
        db.close()

    headers = {"Authorization": "Bearer tk-perm-tampered"}
    response = client.post("/api/sessions/cleanup", json={}, headers=headers)
    assert response.status_code == 403
    assert response.json()["error_detail"]["role"] == auth_service.CUSTOMER_ROLE


def test_unknown_or_missing_identity_is_denied(client):
    """无法确认身份时按未登录处理，不因为「角色取值看不懂」而放行。"""
    for headers in ({}, {"Authorization": "Bearer tk-perm-unknown"}, {"Authorization": "Bearer forged.token.value"}):
        response = client.get("/api/tickets", headers=headers)
        assert response.status_code == 401
        assert response.json()["error_code"] == TicketErrorCode.UNAUTHORIZED


# ---------------------------------------------------------------------------
# 服务层判定：角色口径只有一处，脏数据与空入参都不放宽
# ---------------------------------------------------------------------------


def test_role_helpers_deny_unknown_and_empty_inputs():
    """角色辅助函数对空身份、未知角色与空角色清单一律判否。"""
    assert auth_service.role_of(None) == auth_service.CUSTOMER_ROLE
    assert auth_service.normalize_role("SuperAdmin") == auth_service.CUSTOMER_ROLE
    assert auth_service.normalize_role("  ADMIN  ") == auth_service.ADMIN_ROLE

    customer = {"id": CUSTOMER_ID, "username": "perm-cust", "role": "customer"}
    assert auth_service.is_admin(customer) is False
    assert auth_service.is_admin(None) is False
    assert auth_service.is_customer(None) is False
    assert auth_service.has_role(customer) is False
    assert auth_service.has_role(None, auth_service.ADMIN_ROLE) is False

    with pytest.raises(auth_service.TokenMissingError):
        auth_service.require_login(None)
    with pytest.raises(auth_service.TokenMissingError):
        auth_service.require_admin(None)
    with pytest.raises(auth_service.PermissionDeniedError) as denied:
        auth_service.require_admin(customer)
    assert denied.value.http_status == 403

    # 数据范围：客户永远只有本人，未登录直接拒绝，管理员才可能拿到全量
    assert auth_service.scope_user_id(customer, include_admin_all=True) == CUSTOMER_ID
    assert auth_service.scope_user_id({"id": ADMIN_ID, "role": "admin"}) is None
    assert auth_service.scope_user_id({"id": ADMIN_ID, "role": "admin"}, include_admin_all=False) == ADMIN_ID
    with pytest.raises(auth_service.TokenMissingError):
        auth_service.scope_user_id(None)
