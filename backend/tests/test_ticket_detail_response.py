"""工单详情组装与响应测试。

详情接口的数据组装已从查询流程中拆出，这里分两层验证：
1. 组装层：build_status_timeline / build_ticket_detail / build_detail_summary
   直接吃内存对象，不依赖数据库，验证分区字段、排序与指标汇总；
2. 响应层：get_ticket 的读库组装与详情接口的权限、错误与摘要文案。

测试使用独立的临时 SQLite 库，不污染真实 app.db。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api import tickets as api_tickets
from backend.database import Base
from backend.models import Session, User
from backend.models.ticket import Ticket, TicketStatusLog
from backend.services import auth as auth_service
from backend.services import tickets


def _ticket(
    ticket_id: str = "T-2001",
    status: str = "open",
    tried_steps: str = "重启客户端 · 检查网络",
    evidence: str = "导出报错源于权限不足（来源：产品手册第 3 节）",
    replies=None,
    created_at: float = 1_700_000_000.0,
) -> Ticket:
    """构造不入库的工单对象，用于验证纯组装逻辑。"""
    return Ticket(
        id=ticket_id,
        dedup_key=f"k-{ticket_id}",
        user_id="u-detail",
        user_name="详情用户",
        description="专业版导出失败",
        tried_steps=tried_steps,
        evidence=evidence,
        status=status,
        created_at=created_at,
        replies=[] if replies is None else replies,
    )


def _log(log_id: int, from_status: str, to_status: str, changed_at: float) -> TicketStatusLog:
    """构造不入库的状态日志对象，用于验证时间线排序。"""
    return TicketStatusLog(
        id=log_id,
        ticket_id="T-2001",
        from_status=from_status,
        to_status=to_status,
        operator="system",
        changed_at=changed_at,
    )


# --------------------------------------------------------------------------
# 组装层：状态时间线
# --------------------------------------------------------------------------


def test_状态时间线按变更时间升序排列():
    """乱序入参按变更时间重新排序，输出仍为升序。"""
    logs = [
        _log(3, "open", "resolved", 300.0),
        _log(1, "", "open", 100.0),
        _log(2, "open", "in_progress", 200.0),
    ]

    timeline = tickets.build_status_timeline(logs)

    assert [item["to_status"] for item in timeline] == ["open", "in_progress", "resolved"]


def test_状态时间线同刻按记录号升序():
    """变更时间相同时以记录号为次序兜底，保证顺序稳定。"""
    logs = [
        _log(9, "open", "closed", 100.0),
        _log(2, "", "open", 100.0),
    ]

    timeline = tickets.build_status_timeline(logs)

    assert [item["id"] for item in timeline] == [2, 9]


def test_状态时间线空入参返回空列表():
    """没有状态记录时返回空列表，不抛错。"""
    assert tickets.build_status_timeline(None) == []
    assert tickets.build_status_timeline([]) == []


# --------------------------------------------------------------------------
# 组装层：详情结构与指标汇总
# --------------------------------------------------------------------------


def test_详情组装包含基础字段与三个分区():
    """详情响应在工单基础字段外，稳定给出排查信息、状态时间线与回复时间线。"""
    data = tickets.build_ticket_detail(_ticket(), [_log(1, "", "open", 100.0)])

    assert data["id"] == "T-2001"
    assert data["status"] == "open"
    # 原始文本字段照旧返回，兼容既有展示
    assert data["tried_steps"] == "重启客户端 · 检查网络"
    assert data["evidence"].startswith("导出报错源于权限不足")
    assert data["troubleshooting"]["tried_step_count"] == 2
    assert data["troubleshooting"]["evidence"][0]["source"] == "产品手册第 3 节"
    assert [item["to_status"] for item in data["status_timeline"]] == ["open"]
    assert data["reply_timeline"]["total"] == 0


def test_详情汇总指标取自各分区计数():
    """汇总指标与三个分区的统计结果一致，状态链路按流转顺序给出。"""
    ticket = _ticket(
        created_at=100.0,
        replies=[
            {"replier": "system", "content": "转人工：超出服务范围", "at": 100.0},
            {"replier": "客服小李", "content": "已为您重置授权", "at": 160.0},
        ],
    )
    logs = [_log(1, "", "open", 100.0), _log(2, "open", "transferred_human", 120.0)]

    data = tickets.build_ticket_detail(ticket, logs)
    summary = data["detail_summary"]

    assert summary["tried_step_count"] == 2
    assert summary["evidence_count"] == 1
    assert summary["status_change_count"] == 2
    assert summary["status_chain"] == ["open", "transferred_human"]
    assert summary["last_changed_at"] == 120.0
    assert summary["reply_count"] == 2
    assert summary["human_reply_count"] == 1
    assert summary["system_reply_count"] == 1
    assert summary["has_human_reply"] is True
    # 建单时间 100 到首条人工回复 160，间隔 60 秒
    assert summary["first_response_seconds"] == 60


def test_详情汇总在无记录时给出空指标():
    """空工单的汇总指标为零值，最近变更时间为空。"""
    data = tickets.build_ticket_detail(_ticket(tried_steps="", evidence=""), [])
    summary = data["detail_summary"]

    assert summary["tried_step_count"] == 0
    assert summary["evidence_count"] == 0
    assert summary["status_change_count"] == 0
    assert summary["status_chain"] == []
    assert summary["last_changed_at"] is None
    assert summary["reply_count"] == 0
    assert summary["first_response_seconds"] is None


def test_详情组装不修改原始工单字段():
    """组装只读工单属性，不会把分区字段写回工单对象。"""
    ticket = _ticket()

    tickets.build_ticket_detail(ticket, [_log(1, "", "open", 100.0)])

    assert not hasattr(ticket, "troubleshooting")
    assert ticket.status == "open"


# --------------------------------------------------------------------------
# 响应层：读库组装
# --------------------------------------------------------------------------


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """构造指向临时 SQLite 的 sessionmaker，并替换工单服务引用的 SessionLocal。"""
    db_file = tmp_path / "detail.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(tickets, "SessionLocal", test_session)

    yield test_session

    engine.dispose()


def test_详情查询不存在的工单返回空(db_env):
    """工单不存在时返回 None，由接口层转成「工单不存在」。"""
    assert tickets.get_ticket("T-不存在") is None


def test_详情查询按库内数据组装(db_env):
    """读库路径把工单与状态日志一起组装，分区与汇总结果保持一致。"""
    created, _ = tickets.create_ticket(
        user_id="u-detail",
        user_name="详情用户",
        description="专业版导出失败",
        tried_steps="重启客户端 · 检查网络",
        evidence="导出报错源于权限不足（来源：产品手册第 3 节）",
    )
    tickets.confirm_ticket(created["id"])

    data = tickets.get_ticket(created["id"])

    assert data["status"] == "open"
    assert [item["to_status"] for item in data["status_timeline"]] == [
        "pending_confirm",
        "open",
    ]
    assert data["detail_summary"]["status_chain"] == ["pending_confirm", "open"]
    assert data["detail_summary"]["status_change_count"] == 2
    assert data["detail_summary"]["tried_step_count"] == 2
    assert data["detail_summary"]["reply_count"] == 0


# --------------------------------------------------------------------------
# 响应层：详情接口
# --------------------------------------------------------------------------


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """构造只挂载工单路由的测试应用，预置普通用户、另一用户与管理员三种登录态。"""
    db_file = tmp_path / "detail_api.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    for module in (api_tickets, tickets, auth_service):
        monkeypatch.setattr(module, "SessionLocal", test_session)

    db = test_session()
    try:
        users = [
            ("u-api", "api-user", "接口用户", "customer", "tk-api"),
            ("u-other", "other-user", "他人用户", "customer", "tk-other"),
            ("u-admin", "admin-user", "管理员", "admin", "tk-admin"),
        ]
        for user_id, username, name, role, token in users:
            db.add(
                User(
                    id=user_id,
                    username=username,
                    password_hash="x",
                    name=name,
                    role=role,
                )
            )
            db.add(
                Session(
                    token=token,
                    user_id=user_id,
                    username=username,
                    name=name,
                    role=role,
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


def _seed_ticket(test_session, ticket_id: str = "T-3001", user_id: str = "u-api") -> None:
    """直接落一张待处理工单及其状态日志，便于验证详情响应。"""
    db = test_session()
    try:
        db.add(
            Ticket(
                id=ticket_id,
                dedup_key=f"k-{ticket_id}",
                user_id=user_id,
                user_name="接口用户",
                description="专业版导出失败",
                tried_steps="重启客户端 · 检查网络",
                evidence="导出报错源于权限不足（来源：产品手册第 3 节）",
                status="open",
                created_at=time.time() - 120,
                replies=[{"replier": "客服小李", "content": "已为您重置授权", "at": time.time() - 60}],
            )
        )
        db.add(
            TicketStatusLog(
                ticket_id=ticket_id,
                from_status="",
                to_status="open",
                operator="system",
                changed_at=time.time() - 120,
            )
        )
        db.commit()
    finally:
        db.close()


def test_详情接口返回分区结构与摘要文案(api_client):
    """详情接口返回三个分区与汇总指标，摘要文案回显步骤数与回复分布。"""
    client, test_session = api_client
    _seed_ticket(test_session)
    headers = {"Authorization": "Bearer tk-api"}

    body = client.get("/api/tickets/T-3001", headers=headers).json()

    data = body["ticket"]
    assert data["detail_summary"]["status_chain"] == ["open"]
    assert data["detail_summary"]["human_reply_count"] == 1
    assert data["reply_timeline"]["items"][0]["replier"] == "客服小李"
    assert data["troubleshooting"]["evidence_count"] == 1
    assert body["message"] == "工单 T-3001，留存 2 条已尝试步骤、1 条排查依据，共 1 条回复（人工 1 条、系统 0 条）"


def test_详情接口空分区给出暂无提示(api_client):
    """没有步骤与回复时，摘要文案给出「暂无」提示而不是空白。"""
    client, test_session = api_client
    _seed_ticket(test_session, ticket_id="T-3002")
    db = test_session()
    try:
        row = db.query(Ticket).filter(Ticket.id == "T-3002").first()
        row.tried_steps = ""
        row.evidence = ""
        row.replies = []
        db.commit()
    finally:
        db.close()

    body = client.get("/api/tickets/T-3002", headers={"Authorization": "Bearer tk-api"}).json()

    assert body["message"] == "工单 T-3002，暂无已尝试步骤与排查依据，暂无回复记录"
    assert body["ticket"]["detail_summary"]["reply_count"] == 0


def test_详情接口未登录与不存在(api_client):
    """未登录返回未登录，工单不存在返回工单不存在。"""
    client, _ = api_client

    assert client.get("/api/tickets/T-3001").json() == {"error": "未登录"}
    assert client.get(
        "/api/tickets/T-不存在", headers={"Authorization": "Bearer tk-api"}
    ).json() == {"error": "工单不存在"}


def test_详情接口越权拒绝与管理员放行(api_client):
    """普通用户访问他人工单被拒绝，管理员可查看任意工单。"""
    client, test_session = api_client
    _seed_ticket(test_session, ticket_id="T-3003", user_id="u-other")

    denied = client.get("/api/tickets/T-3003", headers={"Authorization": "Bearer tk-api"}).json()
    assert denied == {"error": "无权限"}

    allowed = client.get(
        "/api/tickets/T-3003", headers={"Authorization": "Bearer tk-admin"}
    ).json()
    assert allowed["ticket"]["id"] == "T-3003"
    assert allowed["message"].startswith("工单 T-3003")
