"""工单模块回归测试。

回归测试盯的是「跨功能的不变量」，而不是单个功能的分支细节——那些由各功能
自己的用例覆盖。这里把已经上线的工单能力串成一张网，任何一处改动破坏了既有
契约，都会在下面几条里暴露出来：

1. 状态机全矩阵：6 个状态 × 6 个目标共 36 种组合，接口表现必须与合法流转表一致；
2. 数据口径一致：列表总数、状态筛选、分页拼接、统计计数、待确认列表互相能对上；
3. 响应字段稳定：列表项、详情、待确认、统计的关键字段一旦缺失，前端就会白屏；
4. 写操作留痕：每次状态变化恰好追加一条时间线，且前后首尾连贯；
5. 权限矩阵：匿名 / 本人 / 他人 / 管理员四种身份在各接口上的结果固定。

全部用例使用独立临时 SQLite，不接触真实 app.db。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import tickets as api_tickets
from backend.database import Base
from backend.models import Order, Product, Session, Ticket, User
from backend.models.ticket import TicketStatus, can_transition
from backend.services import auth as auth_service
from backend.services import orders as orders_service
from backend.tests.reference_api import ticket_service as ticket_service

ADMIN = {"Authorization": "Bearer tk-reg-admin"}
CUSTOMER = {"Authorization": "Bearer tk-reg-customer"}
OTHER = {"Authorization": "Bearer tk-reg-other"}

# 把工单从草稿推到目标状态所需的接口调用序列。
STATE_STEPS = {
    TicketStatus.PENDING_CONFIRM: (),
    TicketStatus.OPEN: ("confirm",),
    TicketStatus.IN_PROGRESS: ("confirm", "reply"),
    TicketStatus.TRANSFERRED_HUMAN: ("confirm", "transfer"),
    TicketStatus.RESOLVED: ("confirm", "resolved"),
    TicketStatus.CLOSED: ("confirm", "closed"),
}

# 状态机契约快照：业务约定的合法流转，刻意与实现分开写。
# 表里多放行或少放行一条，矩阵用例都会立刻报错，避免契约被顺手改掉。
EXPECTED_TRANSITIONS = {
    "pending_confirm": {"open", "closed"},
    "open": {"in_progress", "resolved", "transferred_human", "closed"},
    "in_progress": {"resolved", "transferred_human", "closed"},
    "transferred_human": {"in_progress", "resolved", "closed"},
    "resolved": {"closed"},
    "closed": set(),
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 两个客户 + 一个管理员，挂载工单路由。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'regression.db'}", connect_args={"check_same_thread": False}
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
                User(id="u-admin", username="reg-admin", password_hash="x", name="客服小张", role="admin"),
                User(id="u-cust", username="reg-cust", password_hash="x", name="客户小李", role="customer"),
                User(id="u-other", username="reg-other", password_hash="x", name="客户老王", role="customer"),
                Session(token="tk-reg-admin", user_id="u-admin", username="reg-admin",
                        name="客服小张", role="admin", expires_at=now + 3600),
                Session(token="tk-reg-customer", user_id="u-cust", username="reg-cust",
                        name="客户小李", role="customer", expires_at=now + 3600),
                Session(token="tk-reg-other", user_id="u-other", username="reg-other",
                        name="客户老王", role="customer", expires_at=now + 3600),
                Product(id="pro", name="专业版", features=["导出", "批量处理"]),
                Order(id="o-reg", user_id="u-cust", product_tier="pro", status="active",
                      started_at=now - 86400, expires_at=now + 86400 * 30),
                Order(id="o-reg-other", user_id="u-other", product_tier="pro", status="active",
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


def _create(client, description: str, headers=CUSTOMER) -> str:
    """建单并返回工单号；草稿初始状态固定为待确认。"""
    response = client.post(
        "/api/tickets",
        json={"description": description, "tried_steps": "重启客户端", "evidence": "日志无异常"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["ticket"]["status"] == TicketStatus.PENDING_CONFIRM.value
    return body["ticket"]["id"]


def _drive(client, ticket_id: str, steps, owner=CUSTOMER) -> None:
    """按步骤序列把工单推到目标状态，任一步失败即中断。"""
    for step in steps:
        if step == "confirm":
            response = client.post("/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=owner)
        elif step == "reply":
            response = client.post(
                "/api/tickets/reply",
                json={"ticket_id": ticket_id, "content": "已收到，正在排查"},
                headers=ADMIN,
            )
        elif step == "transfer":
            response = client.post(
                "/api/tickets/transfer",
                json={"ticket_id": ticket_id, "reason": "需要人工介入"},
                headers=ADMIN,
            )
        else:
            response = client.post(
                "/api/tickets/status",
                json={"ticket_id": ticket_id, "status": step},
                headers=ADMIN,
            )
        assert response.status_code == 200, f"{ticket_id} 流转到 {step} 失败：{response.text}"


def _ticket_in_state(client, state: TicketStatus, description: str, owner=CUSTOMER) -> str:
    """新建一张工单并推到指定状态。"""
    ticket_id = _create(client, description, headers=owner)
    _drive(client, ticket_id, STATE_STEPS[state], owner=owner)
    detail = client.get(f"/api/tickets/{ticket_id}", headers=ADMIN).json()["ticket"]
    assert detail["status"] == state.value
    return ticket_id


def _seed_all_states(client) -> dict[str, int]:
    """按固定分布铺一批工单，供口径一致性用例使用。"""
    plan = {
        TicketStatus.PENDING_CONFIRM: 2,
        TicketStatus.OPEN: 3,
        TicketStatus.IN_PROGRESS: 2,
        TicketStatus.TRANSFERRED_HUMAN: 1,
        TicketStatus.RESOLVED: 2,
        TicketStatus.CLOSED: 2,
    }
    for state, count in plan.items():
        for index in range(count):
            description = f"{state.value}-回归-{index}"
            # 埋一个唯一词，用于验证关键词搜索只命中目标工单
            if state is TicketStatus.OPEN and index == 0:
                description += "-ZETA"
            _ticket_in_state(client, state, description)
    return {state.value: count for state, count in plan.items()}


def test_状态机全矩阵与合法流转表一致(client):
    """36 种状态组合逐一验证：该放行的放行，该拒绝的给出统一错误码与前后状态。"""
    for current in TicketStatus:
        allowed = EXPECTED_TRANSITIONS[current.value]
        for target in TicketStatus:
            should_pass = target.value in allowed
            assert can_transition(current, target) is should_pass, (
                f"流转表与契约快照不符：{current.value} -> {target.value}"
            )

            ticket_id = _ticket_in_state(client, current, f"矩阵-{current.value}-到-{target.value}")
            response = client.post(
                "/api/tickets/status",
                json={"ticket_id": ticket_id, "status": target.value},
                headers=ADMIN,
            )

            if should_pass:
                assert response.status_code == 200, f"{current.value} -> {target.value} 应放行：{response.text}"
                assert response.json()["ticket"]["status"] == target.value
                continue

            assert response.status_code == 409, f"{current.value} -> {target.value} 应拒绝：{response.text}"
            payload = response.json()
            assert payload["error_code"] == "TICKET_INVALID_TRANSITION"
            assert payload["error_detail"] == {
                "from_status": current.value,
                "to_status": target.value,
            }
            # 拒绝后工单状态保持原样，非法请求不写库
            after = client.get(f"/api/tickets/{ticket_id}", headers=ADMIN).json()["ticket"]
            assert after["status"] == current.value


def test_列表统计与待确认三处口径一致(client):
    """同一批数据在列表、状态筛选、统计与待确认列表里必须对得上。"""
    plan = _seed_all_states(client)
    expected_total = sum(plan.values())

    full = client.get("/api/tickets", params={"page_size": 100}, headers=ADMIN).json()
    stats = client.get("/api/tickets/stats", headers=ADMIN).json()["stats"]
    assert full["total"] == stats["total"] == expected_total
    assert sum(stats["status_counts"].values()) == expected_total

    for status in TicketStatus:
        filtered = client.get(
            "/api/tickets",
            params={"status": status.value, "page_size": 100},
            headers=ADMIN,
        ).json()
        assert filtered["total"] == plan[status.value], f"{status.value} 筛选数量不一致"
        assert filtered["statuses"] == [status.value]

    pending = client.get(
        "/api/tickets/pending", params={"all_users": True}, headers=ADMIN
    ).json()
    assert pending["total"] == plan[TicketStatus.PENDING_CONFIRM.value]
    assert all(item["status"] == "pending_confirm" for item in pending["tickets"])

    # 多状态筛选等价于各单状态之和
    multi = client.get(
        "/api/tickets",
        params=[("status", "open"), ("status", "closed"), ("page_size", 100)],
        headers=ADMIN,
    ).json()
    assert multi["total"] == plan["open"] + plan["closed"]

    # 分页拼接与一次性取回完全一致：不重复、不遗漏、顺序稳定
    first_page = client.get("/api/tickets", params={"page_size": 5, "page": 1}, headers=ADMIN).json()
    assert first_page["pages"] == (expected_total + 4) // 5
    collected: list[str] = []
    for page in range(1, first_page["pages"] + 1):
        chunk = client.get(
            "/api/tickets", params={"page_size": 5, "page": page}, headers=ADMIN
        ).json()
        collected.extend(item["id"] for item in chunk["tickets"])
        assert chunk["has_more"] is (page < first_page["pages"])
    assert collected == [item["id"] for item in full["tickets"]]
    assert len(set(collected)) == expected_total

    # 正逆序互为倒序
    ascending = client.get(
        "/api/tickets", params={"page_size": 100, "order": "asc"}, headers=ADMIN
    ).json()
    assert [item["id"] for item in ascending["tickets"]] == list(reversed(collected))

    # 关键词只命中目标工单，且工单号与客户名同样可搜
    by_keyword = client.get("/api/tickets", params={"keyword": "zeta"}, headers=ADMIN).json()
    assert by_keyword["total"] == 1 and by_keyword["keyword"] == "zeta"
    by_id = client.get("/api/tickets", params={"keyword": collected[0]}, headers=ADMIN).json()
    assert by_id["total"] == 1 and by_id["tickets"][0]["id"] == collected[0]
    by_name = client.get("/api/tickets", params={"keyword": "小李", "page_size": 100}, headers=ADMIN).json()
    assert by_name["total"] == expected_total, "客户名搜索应覆盖该客户全部工单"


def test_列表与详情的响应字段保持稳定(client):
    """字段一旦被删或改名，前端渲染会直接白屏，这里把契约钉住。"""
    ticket_id = _ticket_in_state(client, TicketStatus.IN_PROGRESS, "字段契约-处理中")
    _create(client, "字段契约-仍待确认")

    listing = client.get("/api/tickets", params={"page_size": 100}, headers=ADMIN).json()
    assert set(listing) == {
        "tickets", "total", "page", "page_size", "pages",
        "has_more", "order", "statuses", "keyword", "message",
    }
    item = listing["tickets"][0]
    assert set(item) == {
        "id", "dedup_key", "user_id", "user_name", "description",
        "tried_steps", "evidence", "status", "created_at", "replies",
    }

    pending_item = client.get(
        "/api/tickets/pending", params={"all_users": True}, headers=ADMIN
    ).json()["tickets"][0]
    assert {"expires_at", "remaining_seconds", "expired"} <= set(pending_item)
    assert pending_item["remaining_seconds"] > 0 and pending_item["expired"] is False

    detail = client.get(f"/api/tickets/{ticket_id}", headers=ADMIN).json()
    assert set(detail) == {"ticket", "message"}
    ticket = detail["ticket"]
    assert {"troubleshooting", "status_timeline", "reply_timeline", "detail_summary"} <= set(ticket)
    assert set(ticket["troubleshooting"]) == {
        "tried_steps", "evidence", "tried_step_count",
        "evidence_count", "has_tried_steps", "has_evidence",
    }
    assert set(ticket["detail_summary"]) == {
        "tried_step_count", "evidence_count", "status_change_count", "status_chain",
        "last_changed_at", "reply_count", "human_reply_count", "system_reply_count",
        "has_human_reply", "first_response_seconds", "transfer_count",
    }
    assert {"items", "total", "human_count", "system_count", "has_replies",
            "transfer_count", "truncated"} <= set(ticket["reply_timeline"])
    assert {"index", "replier", "content", "at", "at_text", "source", "is_human"} <= set(
        ticket["reply_timeline"]["items"][0]
    )

    stats = client.get("/api/tickets/stats", headers=ADMIN).json()["stats"]
    assert set(stats) == {
        "total", "status_counts", "status_breakdown", "active",
        "settled", "handling_duration", "range", "user_id", "generated_at",
    }
    assert set(stats["status_counts"]) == {status.value for status in TicketStatus}
    assert len(stats["status_breakdown"]) == len(TicketStatus)

    order = client.get("/api/order", headers=CUSTOMER).json()["order"]
    assert set(order) == {
        "has_order", "order_id", "product_tier", "product_name",
        "in_service", "features", "expires_at",
    }
    assert order["has_order"] is True and order["in_service"] is True


def test_建单幂等与状态时间线连贯(client):
    """重复建单不产生新单；每次状态变化恰好一条时间线，且首尾逐条连贯。"""
    first = client.post("/api/tickets", json={"description": "导出按钮点击无响应"}, headers=CUSTOMER).json()
    again = client.post("/api/tickets", json={"description": "导出按钮点击无响应"}, headers=CUSTOMER).json()
    assert first["created"] is True and again["created"] is False
    assert first["ticket"]["id"] == again["ticket"]["id"]

    ticket_id = first["ticket"]["id"]
    _drive(client, ticket_id, ("confirm", "reply"))
    # 已在处理中，再次回复只追加内容，不产生新的状态流转
    client.post(
        "/api/tickets/reply",
        json={"ticket_id": ticket_id, "content": "已联系后端排查"},
        headers=ADMIN,
    )
    _drive(client, ticket_id, ("resolved", "closed"))

    ticket = client.get(f"/api/tickets/{ticket_id}", headers=ADMIN).json()["ticket"]
    timeline = ticket["status_timeline"]
    assert [item["to_status"] for item in timeline] == [
        "pending_confirm", "open", "in_progress", "resolved", "closed",
    ]
    assert timeline[0]["from_status"] == ""
    for previous, current in zip(timeline, timeline[1:]):
        assert current["from_status"] == previous["to_status"]
    assert all(current["changed_at"] >= previous["changed_at"] for previous, current in zip(timeline, timeline[1:]))

    summary = ticket["detail_summary"]
    assert summary["status_change_count"] == len(timeline)
    assert summary["status_chain"] == [item["to_status"] for item in timeline]
    assert summary["reply_count"] == 2, "两次人工回复都应保留，状态不变时不再写时间线"
    assert summary["human_reply_count"] == 2
    assert ticket["reply_timeline"]["total"] == 2


def test_权限矩阵覆盖全部写接口(client):
    """匿名、本人、他人、管理员四种身份在每个接口上的结果固定不变。"""
    mine = _ticket_in_state(client, TicketStatus.OPEN, "权限矩阵-本人工单")
    foreign_id = _ticket_in_state(client, TicketStatus.OPEN, "权限矩阵-他人工单", owner=OTHER)

    # 匿名：除订单查询外，工单接口一律 401 且错误体一致
    anonymous_calls = [
        ("get", "/api/tickets", {}),
        ("post", "/api/tickets", {"json": {"description": "匿名建单"}}),
        ("get", "/api/tickets/pending", {}),
        ("get", "/api/tickets/stats", {}),
        ("get", f"/api/tickets/{mine}", {}),
        ("post", "/api/tickets/confirm", {"json": {"ticket_id": mine}}),
        ("post", "/api/tickets/cancel", {"json": {"ticket_id": mine}}),
        ("post", "/api/tickets/reply", {"json": {"ticket_id": mine, "content": "x"}}),
        ("post", "/api/tickets/status", {"json": {"ticket_id": mine, "status": "closed"}}),
        ("post", "/api/tickets/transfer", {"json": {"ticket_id": mine, "reason": "x"}}),
        ("post", "/api/tickets/expire", {}),
    ]
    for method, path, kwargs in anonymous_calls:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, f"{method.upper()} {path} 未拒绝匿名"
        assert response.json() == {"error": "未登录", "error_code": "TICKET_UNAUTHORIZED"}

    # 普通用户：管理动作一律 403
    for path, kwargs in (
        ("/api/tickets/reply", {"json": {"ticket_id": mine, "content": "越权回复"}}),
        ("/api/tickets/status", {"json": {"ticket_id": mine, "status": "closed"}}),
        ("/api/tickets/transfer", {"json": {"ticket_id": mine, "reason": "越权转人工"}}),
        ("/api/tickets/expire", {}),
        ("/api/tickets/batch-close", {"json": {"ticket_ids": [mine]}}),
        ("/api/tickets/batch-transfer", {"json": {"ticket_ids": [mine]}}),
    ):
        response = client.post(path, headers=CUSTOMER, **kwargs)
        assert response.status_code == 403, f"{path} 未拒绝普通用户"
        assert response.json() == {"error": "无权限", "error_code": "TICKET_FORBIDDEN"}

    # 普通用户：他人工单不可查看、确认、取消
    for method, path, kwargs in (
        ("get", f"/api/tickets/{foreign_id}", {}),
        ("get", f"/api/tickets/{foreign_id}/transfer-decision", {}),
        ("post", "/api/tickets/confirm", {"json": {"ticket_id": foreign_id}}),
        ("post", "/api/tickets/cancel", {"json": {"ticket_id": foreign_id}}),
    ):
        response = getattr(client, method)(path, headers=CUSTOMER, **kwargs)
        assert response.status_code == 403, f"{method.upper()} {path} 未拒绝跨用户访问"
        assert response.json()["error_code"] == "TICKET_FORBIDDEN"

    # 数据范围：普通用户只看本人，管理员看全量
    mine_total = client.get("/api/tickets", headers=CUSTOMER).json()["total"]
    admin_total = client.get("/api/tickets", headers=ADMIN).json()["total"]
    other_total = client.get("/api/tickets", headers=OTHER).json()["total"]
    assert admin_total == mine_total + other_total
    assert mine_total == 1 and other_total == 1
    assert client.get(f"/api/tickets/{foreign_id}", headers=ADMIN).status_code == 200

    # 普通用户统计只能看本人，指定他人工号被拒绝
    own_stats = client.get("/api/tickets/stats", headers=CUSTOMER).json()["stats"]
    assert own_stats["total"] == mine_total
    denied = client.get("/api/tickets/stats", params={"user_id": "u-other"}, headers=CUSTOMER)
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "TICKET_FORBIDDEN"


def test_错误响应结构在回归中保持不变(client):
    """不存在、非法流转、非法分页与非法关键词都返回同一套错误体。"""
    missing = client.get("/api/tickets/T-9999", headers=ADMIN)
    assert missing.status_code == 404
    assert missing.json() == {
        "error": "工单不存在",
        "error_code": "TICKET_NOT_FOUND",
        "error_detail": {"ticket_id": "T-9999"},
    }

    ticket_id = _ticket_in_state(client, TicketStatus.CLOSED, "错误体-已关闭")
    illegal = client.post(
        "/api/tickets/status",
        json={"ticket_id": ticket_id, "status": "open"},
        headers=ADMIN,
    )
    assert illegal.status_code == 409
    assert illegal.json()["error_code"] == "TICKET_INVALID_TRANSITION"

    unknown = client.post(
        "/api/tickets/status",
        json={"ticket_id": ticket_id, "status": "archived"},
        headers=ADMIN,
    )
    assert unknown.status_code == 422
    assert unknown.json()["error_code"] == "TICKET_UNKNOWN_STATUS"

    for params, code in (
        ({"page": 0}, "TICKET_INVALID_PAGINATION"),
        ({"page_size": 101}, "TICKET_INVALID_PAGINATION"),
        ({"status": "archived"}, "TICKET_INVALID_STATUS_FILTER"),
        ({"order": "random"}, "TICKET_INVALID_ORDER"),
        ({"keyword": "长" * 51}, "TICKET_INVALID_KEYWORD"),
    ):
        response = client.get("/api/tickets", params=params, headers=ADMIN)
        assert response.status_code == 422, f"{params} 未按非法入参拒绝"
        payload = response.json()
        assert set(payload) <= {"error", "error_code", "error_detail"}
        assert payload["error_code"] == code
