"""两阶段建单全流程测试。

覆盖「草稿 → 确认 / 取消 / 超时」三条完整链路：
1. 创建 → 确认：草稿流转为待处理，状态时间线落两条；
2. 创建 → 取消：草稿流转为已关闭，取消原因写入回复，时间线落两条；
3. 创建 → 超时：超时扫描批量收敛为已关闭，未到期草稿不受影响；
4. 接口链路：登录态下建单 → 待确认列表 → 确认 → 详情，串起对外接口。

测试使用独立的临时 SQLite 库，不污染真实 app.db。
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
from backend.services import auth as auth_service
from backend.tests.reference_api import ticket_service as tickets


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """构造指向临时 SQLite 的 sessionmaker，并替换工单服务引用的 SessionLocal。"""
    db_file = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(tickets, "SessionLocal", test_session)

    yield test_session

    engine.dispose()


def _create(db_env, description: str, user_id: str = "u-flow") -> str:
    """建一张草稿工单，返回工单号。"""
    ticket, created = tickets.create_ticket(
        user_id=user_id,
        user_name="流程用户",
        description=description,
        tried_steps="重启客户端",
        evidence="日志无异常",
    )
    assert created is True
    assert ticket["status"] == "pending_confirm"
    return ticket["id"]


def _age(db_env, ticket_id: str, seconds: int) -> None:
    """把工单建单时间前移，模拟已放置指定秒数的老草稿。"""
    db = db_env()
    try:
        row = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        row.created_at = time.time() - seconds
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# 创建 → 确认
# --------------------------------------------------------------------------


def test_建单初始落待确认草稿且时间线首条为空原状态(db_env):
    """第一阶段只创建草稿，不直接成为待处理工单。"""
    ticket_id = _create(db_env, "专业版导出失败")

    detail = tickets.get_ticket(ticket_id)
    assert detail["status"] == "pending_confirm"
    assert detail["tried_steps"] == "重启客户端"
    assert detail["evidence"] == "日志无异常"
    assert len(detail["status_timeline"]) == 1
    first = detail["status_timeline"][0]
    assert first["from_status"] == ""
    assert first["to_status"] == "pending_confirm"


def test_确认草稿流转为待处理并落第二条时间线(db_env):
    """确认后草稿转为 open，时间线追加 pending_confirm -> open。"""
    ticket_id = _create(db_env, "专业版导出失败")

    confirmed = tickets.confirm_ticket(ticket_id)

    assert confirmed["status"] == "open"
    detail = tickets.get_ticket(ticket_id)
    timeline = detail["status_timeline"]
    assert [item["to_status"] for item in timeline] == ["pending_confirm", "open"]
    assert timeline[1]["from_status"] == "pending_confirm"


def test_重复确认不产生额外流转(db_env):
    """确认接口幂等：第二次确认原样返回，时间线不再增长。"""
    ticket_id = _create(db_env, "专业版导出失败")
    tickets.confirm_ticket(ticket_id)

    again = tickets.confirm_ticket(ticket_id)

    assert again["status"] == "open"
    assert len(tickets.get_ticket(ticket_id)["status_timeline"]) == 2


def test_确认不存在的工单返回空(db_env):
    """工单不存在时确认返回 None，由接口层转成「工单不存在」。"""
    assert tickets.confirm_ticket("T-不存在") is None


# --------------------------------------------------------------------------
# 创建 → 取消
# --------------------------------------------------------------------------


def test_取消草稿流转为已关闭并记录原因(db_env):
    """取消后草稿转为 closed，取消原因写入回复时间线。"""
    ticket_id = _create(db_env, "专业版导出失败")

    cancelled = tickets.cancel_ticket(ticket_id, reason="问题已自行解决")

    assert cancelled["status"] == "closed"
    assert cancelled["replies"][-1]["content"] == "取消工单：问题已自行解决"
    detail = tickets.get_ticket(ticket_id)
    assert [item["to_status"] for item in detail["status_timeline"]] == [
        "pending_confirm",
        "closed",
    ]


def test_取消后不可再确认(db_env):
    """已取消的草稿再确认时幂等返回 closed，不会重新打开。"""
    ticket_id = _create(db_env, "专业版导出失败")
    tickets.cancel_ticket(ticket_id, reason="不需要了")

    again = tickets.confirm_ticket(ticket_id)

    assert again["status"] == "closed"
    assert len(tickets.get_ticket(ticket_id)["status_timeline"]) == 2


def test_确认后不可再取消(db_env):
    """已确认的工单再取消时幂等返回 open，不追加取消记录。"""
    ticket_id = _create(db_env, "专业版导出失败")
    tickets.confirm_ticket(ticket_id)

    again = tickets.cancel_ticket(ticket_id, reason="迟到的取消")

    assert again["status"] == "open"
    assert again["replies"] == []
    assert len(tickets.get_ticket(ticket_id)["status_timeline"]) == 2


# --------------------------------------------------------------------------
# 创建 → 超时
# --------------------------------------------------------------------------


def test_超时草稿被扫描关闭并写入超时说明(db_env):
    """超过待确认阈值仍未确认的草稿，被收敛为已关闭。"""
    ticket_id = _create(db_env, "专业版导出失败")
    _age(db_env, ticket_id, tickets.PENDING_CONFIRM_TIMEOUT_SECONDS + 60)

    result = tickets.expire_pending_tickets()

    assert result["closed"] == 1
    assert result["closed_ids"] == [ticket_id]
    assert result["remaining"] == 0
    detail = tickets.get_ticket(ticket_id)
    assert detail["status"] == "closed"
    assert "超时" in detail["replies"][-1]["content"]
    assert [item["to_status"] for item in detail["status_timeline"]] == [
        "pending_confirm",
        "closed",
    ]


def test_未超时草稿不被扫描关闭(db_env):
    """仍在确认窗口内的草稿保持待确认，扫描结果为空。"""
    ticket_id = _create(db_env, "专业版导出失败")
    _age(db_env, ticket_id, 10)

    result = tickets.expire_pending_tickets()

    assert result["closed"] == 0
    assert result["scanned"] == 0
    assert tickets.get_ticket(ticket_id)["status"] == "pending_confirm"


def test_超时扫描分批消化并返回剩余数量(db_env):
    """单批上限内的草稿先关闭，超出部分通过 remaining 提示继续下一批。"""
    ids = [_create(db_env, f"专业版导出失败-{i}") for i in range(3)]
    for ticket_id in ids:
        _age(db_env, ticket_id, tickets.PENDING_CONFIRM_TIMEOUT_SECONDS + 60)

    first_batch = tickets.expire_pending_tickets(limit=2)
    assert first_batch["closed"] == 2
    assert first_batch["remaining"] == 1

    second_batch = tickets.expire_pending_tickets(limit=2)
    assert second_batch["closed"] == 1
    assert second_batch["remaining"] == 0


def test_超时关闭后确认幂等不复活(db_env):
    """已被超时关闭的草稿再确认时保持 closed。"""
    ticket_id = _create(db_env, "专业版导出失败")
    _age(db_env, ticket_id, tickets.PENDING_CONFIRM_TIMEOUT_SECONDS + 60)
    tickets.expire_pending_tickets()

    again = tickets.confirm_ticket(ticket_id)

    assert again["status"] == "closed"


def test_待确认列表按时间窗口过滤草稿(db_env):
    """默认只列出仍可确认的草稿，include_expired 时标出已失效条目。"""
    fresh_id = _create(db_env, "专业版导出失败-新")
    stale_id = _create(db_env, "专业版导出失败-旧")
    _age(db_env, stale_id, tickets.PENDING_CONFIRM_TIMEOUT_SECONDS + 60)

    default_list = tickets.list_pending_tickets(user_id="u-flow")
    assert [item["id"] for item in default_list["items"]] == [fresh_id]
    assert default_list["items"][0]["expired"] is False
    assert default_list["items"][0]["remaining_seconds"] > 0

    full_list = tickets.list_pending_tickets(user_id="u-flow", include_expired=True)
    assert {item["id"] for item in full_list["items"]} == {fresh_id, stale_id}
    assert full_list["expired"] == 1


# --------------------------------------------------------------------------
# 接口链路：建单 → 待确认列表 → 确认 → 详情
# --------------------------------------------------------------------------


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """构造只挂载工单路由的测试应用，并预置登录态用户。"""
    db_file = tmp_path / "api.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    for module in (api_tickets, tickets, auth_service):
        monkeypatch.setattr(module, "SessionLocal", test_session)

    db = test_session()
    try:
        db.add(
            User(
                id="u-api",
                username="api-user",
                password_hash="x",
                name="接口用户",
                role="customer",
            )
        )
        db.add(
            Session(
                token="tk-api",
                user_id="u-api",
                username="api-user",
                name="接口用户",
                role="customer",
                expires_at=time.time() + 3600,
            )
        )
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.include_router(api_tickets.tickets_router, prefix="/api")
    client = TestClient(app)

    yield client, test_session

    engine.dispose()


def test_接口建单到确认全链路(api_client):
    """接口串起「建单 → 待确认列表 → 确认 → 详情」完整链路。"""
    client, _ = api_client
    headers = {"Authorization": "Bearer tk-api"}

    created = client.post(
        "/api/tickets",
        json={"description": "专业版导出失败", "tried_steps": "重启客户端", "evidence": ""},
        headers=headers,
    ).json()
    assert created["created"] is True
    assert created["ticket"]["status"] == "pending_confirm"
    ticket_id = created["ticket"]["id"]

    pending = client.get("/api/tickets/pending", headers=headers).json()
    assert pending["total"] == 1
    assert pending["tickets"][0]["id"] == ticket_id
    assert pending["tickets"][0]["expired"] is False

    confirmed = client.post(
        "/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=headers
    ).json()
    assert confirmed["ticket"]["status"] == "open"

    detail = client.get(f"/api/tickets/{ticket_id}", headers=headers).json()
    assert detail["ticket"]["status"] == "open"
    assert [item["to_status"] for item in detail["ticket"]["status_timeline"]] == [
        "pending_confirm",
        "open",
    ]

    # 确认后草稿移出待确认列表
    assert client.get("/api/tickets/pending", headers=headers).json()["total"] == 0


def test_接口建单到取消全链路(api_client):
    """接口建单后取消，工单进入已关闭并保留取消原因。"""
    client, _ = api_client
    headers = {"Authorization": "Bearer tk-api"}

    created = client.post(
        "/api/tickets", json={"description": "专业版导出失败"}, headers=headers
    ).json()
    ticket_id = created["ticket"]["id"]

    cancelled = client.post(
        "/api/tickets/cancel",
        json={"ticket_id": ticket_id, "reason": "已自行解决"},
        headers=headers,
    ).json()
    assert cancelled["ticket"]["status"] == "closed"
    assert cancelled["ticket"]["replies"][-1]["content"] == "取消工单：已自行解决"

    detail = client.get(f"/api/tickets/{ticket_id}", headers=headers).json()
    assert [item["to_status"] for item in detail["ticket"]["status_timeline"]] == [
        "pending_confirm",
        "closed",
    ]
    assert client.get("/api/tickets/pending", headers=headers).json()["total"] == 0


def test_接口未登录拒绝写操作(api_client):
    """未携带登录态时，建单/确认/取消一律返回未登录。"""
    client, test_session = api_client
    db = test_session()
    try:
        db.add(
            Ticket(
                id="T-9001",
                dedup_key="k-9001",
                user_id="u-api",
                user_name="接口用户",
                description="已存在的草稿",
                tried_steps="",
                evidence="",
                status="pending_confirm",
                created_at=time.time(),
                replies=[],
            )
        )
        db.commit()
    finally:
        db.close()

    create_resp = client.post("/api/tickets", json={"description": "无登录态"}).json()
    assert create_resp["error"] == "未登录"
    assert create_resp["error_code"] == "TICKET_UNAUTHORIZED"

    confirm_resp = client.post("/api/tickets/confirm", json={"ticket_id": "T-9001"}).json()
    assert confirm_resp["error"] == "未登录"
    assert confirm_resp["error_code"] == "TICKET_UNAUTHORIZED"

    cancel_resp = client.post(
        "/api/tickets/cancel", json={"ticket_id": "T-9001", "reason": ""}
    ).json()
    assert cancel_resp["error"] == "未登录"
    assert cancel_resp["error_code"] == "TICKET_UNAUTHORIZED"
