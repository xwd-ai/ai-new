"""过期订单与续费提示测试。

覆盖三组：

- 提醒判定：服务中 / 即将到期 / 已到期 / 未开始 / 已退订 / 数据不全 六种情形，
  以及提醒窗口与紧急阈值的边界；
- 提醒汇总：多订单下的计数、顺序与一句话结论，到期与即将到期分开计数；
- 过期归档：只收敛「服务期已结束且状态仍是有效」的订单，分批与按用户过滤，
  归档只改状态不动服务期。

时间一律由用例传入 now，不依赖真实时钟。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api import tickets as api_tickets
from backend.database import Base
from backend.models import Order, Product, Session as LoginSession, User
from backend.services import auth as auth_service
from backend.services import orders
from backend.services import tickets as ticket_service

DAY = 86400


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 产品配置行，服务层的 SessionLocal 指向它。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'renewal.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(orders, "SessionLocal", session_factory)

    db = session_factory()
    try:
        db.add_all(
            [
                Product(id="basic", name="基础版", features=["基础导出", "单文件上传", "社区支持"]),
                Product(id="pro", name="专业版", features=["批量导出", "高级报表"]),
            ]
        )
        db.commit()
    finally:
        db.close()

    yield session_factory
    engine.dispose()


def _add_order(factory, **overrides) -> None:
    """插入一条订单，默认是服务中的专业版订单。"""
    now = time.time()
    payload = {
        "id": "O-7001",
        "user_id": "u-renew",
        "product_tier": "pro",
        "status": "active",
        "started_at": now - 30 * DAY,
        "expires_at": now + 90 * DAY,
    }
    payload.update(overrides)
    db = factory()
    try:
        db.add(Order(**payload))
        db.commit()
    finally:
        db.close()


def _order(order_id: str, **overrides) -> dict:
    """构造一条订单记录，用于纯函数判定。"""
    now = time.time()
    payload = {
        "id": order_id,
        "user_id": "u-renew",
        "product_tier": "pro",
        "status": "active",
        "started_at": now - 30 * DAY,
        "expires_at": now + 90 * DAY,
    }
    payload.update(overrides)
    return payload


def _statuses(factory, ids) -> dict:
    db = factory()
    try:
        return {
            row.id: row.status
            for row in db.query(Order).filter(Order.id.in_(list(ids))).all()
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 一、提醒判定
# ---------------------------------------------------------------------------


def test_no_order_means_no_reminder():
    hint = orders.evaluate_renewal(None)

    assert hint["state"] == orders.RENEWAL_NONE
    assert hint["remind"] is False
    assert hint["order_id"] is None
    assert hint["message"] == "未查询到订单记录"


def test_active_order_far_from_expiry_is_not_reminded():
    now = time.time()
    hint = orders.evaluate_renewal(_order("O-7101", expires_at=now + 90 * DAY), now=now)

    assert hint["state"] == orders.RENEWAL_ACTIVE
    assert hint["remind"] is False
    assert hint["urgency"] == orders.URGENCY_NONE
    assert hint["suggested_action"] == orders.ACTION_NONE
    assert hint["days_remaining"] == 90
    assert "服务中" in hint["message"]


def test_order_within_window_is_reminded():
    now = time.time()
    hint = orders.evaluate_renewal(_order("O-7201", expires_at=now + 20 * DAY), now=now)

    assert hint["state"] == orders.RENEWAL_UPCOMING
    assert hint["remind"] is True
    assert hint["urgency"] == orders.URGENCY_WARNING
    assert hint["suggested_action"] == orders.ACTION_RENEW
    assert "剩余 20 天" in hint["message"]


def test_order_within_urgent_threshold_escalates():
    now = time.time()
    hint = orders.evaluate_renewal(_order("O-7301", expires_at=now + 3 * DAY), now=now)

    assert hint["state"] == orders.RENEWAL_UPCOMING
    assert hint["urgency"] == orders.URGENCY_CRITICAL
    assert "仅剩 3 天" in hint["message"]


def test_window_boundary_is_inclusive():
    """正好落在窗口边界上要提示，超一天就不再提示。"""
    now = time.time()

    inside = orders.evaluate_renewal(
        _order("O-7401", expires_at=now + orders.RENEWAL_WINDOW_DAYS * DAY), now=now
    )
    outside = orders.evaluate_renewal(
        _order("O-7402", expires_at=now + (orders.RENEWAL_WINDOW_DAYS + 1) * DAY), now=now
    )

    assert inside["state"] == orders.RENEWAL_UPCOMING
    assert outside["state"] == orders.RENEWAL_ACTIVE


def test_expired_period_reports_overdue_days():
    now = time.time()
    hint = orders.evaluate_renewal(
        _order("O-7501", started_at=now - 200 * DAY, expires_at=now - 10 * DAY), now=now
    )

    assert hint["state"] == orders.RENEWAL_DUE
    assert hint["remind"] is True
    assert hint["urgency"] == orders.URGENCY_CRITICAL
    assert hint["days_overdue"] == 10
    assert hint["days_remaining"] == 0
    assert "已过期 10 天" in hint["message"]


def test_order_marked_expired_needs_renewal_even_if_period_remains():
    """状态已归档为过期时，即使服务期还没走完也要提示续费。"""
    now = time.time()
    hint = orders.evaluate_renewal(
        _order("O-7601", status="expired", expires_at=now + 30 * DAY), now=now
    )

    assert hint["state"] == orders.RENEWAL_DUE
    assert hint["remind"] is True
    assert hint["order_status"] == "expired"
    assert "已标记为过期" in hint["message"]


def test_cancelled_order_is_not_reminded():
    now = time.time()
    hint = orders.evaluate_renewal(
        _order("O-7701", status="cancelled", started_at=now - 200 * DAY, expires_at=now - 10 * DAY),
        now=now,
    )

    assert hint["state"] == orders.RENEWAL_BLOCKED
    assert hint["remind"] is False
    assert hint["suggested_action"] == orders.ACTION_BUY_NEW
    assert "重新购买" in hint["message"]


def test_not_started_order_is_not_reminded():
    now = time.time()
    hint = orders.evaluate_renewal(
        _order("O-7801", started_at=now + 10 * DAY, expires_at=now + 40 * DAY), now=now
    )

    assert hint["state"] == orders.RENEWAL_NOT_STARTED
    assert hint["remind"] is False
    assert hint["suggested_action"] == orders.ACTION_WAIT
    assert "暂无需续费" in hint["message"]


def test_broken_period_is_reported_as_unknown():
    now = time.time()
    hint = orders.evaluate_renewal(
        _order("O-7901", started_at=now + 10 * DAY, expires_at=now + 5 * DAY), now=now
    )

    assert hint["state"] == orders.RENEWAL_UNKNOWN
    assert hint["remind"] is False
    assert hint["suggested_action"] == orders.ACTION_VERIFY


def test_window_can_be_overridden_per_call():
    now = time.time()
    order = _order("O-7951", expires_at=now + 10 * DAY)

    narrow = orders.evaluate_renewal(order, now=now, window_days=5)
    wide = orders.evaluate_renewal(order, now=now, window_days=100)

    assert narrow["state"] == orders.RENEWAL_ACTIVE
    assert wide["state"] == orders.RENEWAL_UPCOMING
    assert wide["window_days"] == 100


@pytest.mark.parametrize("window", [None, "abc", True, -5, 999])
def test_invalid_window_falls_back_or_clamps(window):
    """非整数回落默认窗口，越界值收敛到 0–365，负窗口不会变成「永远提醒」。"""
    now = time.time()
    hint = orders.evaluate_renewal(
        _order("O-7961", expires_at=now + 20 * DAY), now=now, window_days=window
    )

    assert hint["window_days"] in (0, orders.RENEWAL_WINDOW_DAYS, 365)
    assert 0 <= hint["window_days"] <= 365
    if hint["window_days"] == 0:
        assert hint["state"] == orders.RENEWAL_ACTIVE


def test_hint_carries_display_fields():
    now = time.time()
    hint = orders.evaluate_renewal(_order("O-7971", expires_at=now + 90 * DAY), now=now)

    assert hint["order_id"] == "O-7971"
    assert hint["product_tier"] == "pro"
    assert hint["tier_label"] == "专业版"
    assert hint["expires_text"]
    assert hint["state_label"] == "服务中"


# ---------------------------------------------------------------------------
# 二、提醒汇总
# ---------------------------------------------------------------------------


def test_list_renewal_hints_without_orders(factory):
    result = orders.list_renewal_hints("u-nobody")

    assert result["has_order"] is False
    assert result["order_count"] == 0
    assert result["remind_count"] == 0
    assert result["hints"] == []
    assert result["message"] == "未查询到订单记录，暂无续费提示"


def test_list_renewal_hints_counts_due_and_upcoming(factory):
    now = time.time()
    _add_order(factory, id="O-8101", expires_at=now + 300 * DAY)
    _add_order(factory, id="O-8102", product_tier="basic", expires_at=now + 10 * DAY)
    _add_order(factory, id="O-8103", started_at=now - 400 * DAY, expires_at=now - 20 * DAY)

    result = orders.list_renewal_hints("u-renew", now=now)

    assert result["order_count"] == 3
    assert result["due_count"] == 1
    assert result["upcoming_count"] == 1
    assert result["remind_count"] == 2
    # 顺序沿用到期时间倒序，最近到期与已到期的排在后面
    assert [hint["order_id"] for hint in result["hints"]] == ["O-8101", "O-8102", "O-8103"]
    assert result["message"] == "1 个订单已到期，另有 1 个将在 30 天内到期"


def test_list_renewal_hints_only_upcoming(factory):
    now = time.time()
    _add_order(factory, id="O-8201", expires_at=now + 10 * DAY)

    result = orders.list_renewal_hints("u-renew", now=now)

    assert result["due_count"] == 0
    assert result["message"] == "1 个订单将在 30 天内到期，建议提前续费"


def test_list_renewal_hints_nothing_to_do(factory):
    now = time.time()
    _add_order(factory, id="O-8301", expires_at=now + 200 * DAY)
    _add_order(factory, id="O-8302", product_tier="basic", expires_at=now + 300 * DAY)

    result = orders.list_renewal_hints("u-renew", now=now)

    assert result["remind_count"] == 0
    assert result["message"] == "共 2 个订单，暂无需续费"


def test_list_renewal_hints_respects_custom_window(factory):
    now = time.time()
    _add_order(factory, id="O-8401", expires_at=now + 60 * DAY)

    narrow = orders.list_renewal_hints("u-renew", now=now, window_days=10)
    wide = orders.list_renewal_hints("u-renew", now=now, window_days=90)

    assert narrow["remind_count"] == 0
    assert narrow["window_days"] == 10
    assert wide["remind_count"] == 1


# ---------------------------------------------------------------------------
# 三、过期归档
# ---------------------------------------------------------------------------


def test_expire_archives_only_overdue_active_orders(factory):
    now = time.time()
    _add_order(factory, id="O-9101", started_at=now - 200 * DAY, expires_at=now - 10 * DAY)
    _add_order(factory, id="O-9102", product_tier="basic",
               started_at=now - 100 * DAY, expires_at=now - 1 * DAY)
    _add_order(factory, id="O-9103", expires_at=now + 30 * DAY)
    _add_order(factory, id="O-9104", status="cancelled",
               started_at=now - 100 * DAY, expires_at=now - 5 * DAY)
    _add_order(factory, id="O-9105", status="expired",
               started_at=now - 300 * DAY, expires_at=now - 200 * DAY)

    result = orders.expire_overdue_orders(user_id="u-renew", now=now)

    assert result["archived"] == ["O-9101", "O-9102"]
    assert result["scanned"] == 2
    assert result["remaining"] == 0
    assert result["has_more"] is False
    assert result["message"] == "已归档 2 个到期订单"
    assert _statuses(factory, ["O-9101", "O-9102", "O-9103", "O-9104", "O-9105"]) == {
        "O-9101": "expired",
        "O-9102": "expired",
        "O-9103": "active",
        "O-9104": "cancelled",
        "O-9105": "expired",
    }


def test_expire_keeps_service_period_untouched(factory):
    """归档只改状态，到期时间属于合同事实，不能被顺手改写。"""
    now = time.time()
    expires = now - 10 * DAY
    _add_order(factory, id="O-9201", started_at=now - 200 * DAY, expires_at=expires)

    orders.expire_overdue_orders(user_id="u-renew", now=now)

    db = factory()
    try:
        row = db.query(Order).filter(Order.id == "O-9201").first()
        assert row.status == "expired"
        assert row.expires_at == expires
        assert row.started_at == pytest.approx(now - 200 * DAY)
    finally:
        db.close()


def test_expire_is_batched(factory):
    now = time.time()
    for index in range(3):
        _add_order(factory, id=f"O-93{index}",
                   started_at=now - 200 * DAY, expires_at=now - (index + 1) * DAY)

    first = orders.expire_overdue_orders(user_id="u-renew", now=now, limit=2)
    second = orders.expire_overdue_orders(user_id="u-renew", now=now, limit=2)

    assert first["scanned"] == 2
    assert first["remaining"] == 1
    assert first["has_more"] is True
    # 到期更早的排在前面，先被归档
    assert first["archived"] == ["O-932", "O-931"]
    assert second["archived"] == ["O-930"]
    assert second["has_more"] is False


def test_expire_is_idempotent(factory):
    now = time.time()
    _add_order(factory, id="O-9401", started_at=now - 200 * DAY, expires_at=now - 10 * DAY)

    orders.expire_overdue_orders(user_id="u-renew", now=now)
    again = orders.expire_overdue_orders(user_id="u-renew", now=now)

    assert again["archived"] == []
    assert again["scanned"] == 0
    assert again["message"] == "没有需要归档的到期订单"


def test_expire_scoped_by_user(factory):
    now = time.time()
    _add_order(factory, id="O-9501", started_at=now - 200 * DAY, expires_at=now - 10 * DAY)
    _add_order(factory, id="O-9502", user_id="u-other",
               started_at=now - 200 * DAY, expires_at=now - 10 * DAY)

    result = orders.expire_overdue_orders(user_id="u-renew", now=now)

    assert result["archived"] == ["O-9501"]
    assert _statuses(factory, ["O-9501", "O-9502"]) == {
        "O-9501": "expired",
        "O-9502": "active",
    }


def test_expire_without_candidates(factory):
    now = time.time()
    _add_order(factory, id="O-9601", expires_at=now + 30 * DAY)

    result = orders.expire_overdue_orders(user_id="u-renew", now=now)

    assert result["scanned"] == 0
    assert result["message"] == "没有需要归档的到期订单"


def test_expired_order_disappears_from_reminder_due_to_active_check(factory):
    """归档后服务范围判定立即变为不可用，提醒也随之转为已到期。"""
    now = time.time()
    _add_order(factory, id="O-9701", started_at=now - 200 * DAY, expires_at=now - 3 * DAY)

    before = orders.list_renewal_hints("u-renew", now=now)
    orders.expire_overdue_orders(user_id="u-renew", now=now)
    after = orders.list_renewal_hints("u-renew", now=now)

    assert before["hints"][0]["state"] == orders.RENEWAL_DUE
    assert after["hints"][0]["state"] == orders.RENEWAL_DUE
    assert after["hints"][0]["remind"] is True
    assert orders.describe_order("u-renew", now=now)["allowed"] is False


# ---------------------------------------------------------------------------
# 四、接口层
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 预置管理员与客户，挂载工单路由。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'renewal-api.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, ticket_service, auth_service, orders):
        monkeypatch.setattr(module, "SessionLocal", factory)

    now = time.time()
    db = factory()
    try:
        db.add_all(
            [
                User(id="u-admin", username="renew-admin", password_hash="x", name="客服小张", role="admin"),
                User(id="u-cust", username="renew-cust", password_hash="x", name="客户小李", role="customer"),
                LoginSession(token="tk-renew-admin", user_id="u-admin", username="renew-admin",
                             name="客服小张", role="admin", expires_at=now + 3600),
                LoginSession(token="tk-renew-cust", user_id="u-cust", username="renew-cust",
                             name="客户小李", role="customer", expires_at=now + 3600),
                Product(id="pro", name="专业版", features=["导出", "批量处理"]),
                Order(id="O-api-1", user_id="u-cust", product_tier="pro", status="active",
                      started_at=now - 200 * DAY, expires_at=now - 5 * DAY),
                Order(id="O-api-2", user_id="u-cust", product_tier="pro", status="active",
                      started_at=now - 10 * DAY, expires_at=now + 10 * DAY),
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


CUST = {"Authorization": "Bearer tk-renew-cust"}
ADMIN = {"Authorization": "Bearer tk-renew-admin"}


def test_renewal_endpoint_returns_hints(client):
    response = client.get("/api/order/renewal", headers=CUST)

    assert response.status_code == 200
    payload = response.json()
    assert payload["order_count"] == 2
    assert payload["due_count"] == 1
    assert payload["upcoming_count"] == 1
    assert payload["window_days"] == orders.RENEWAL_WINDOW_DAYS
    assert {hint["order_id"] for hint in payload["hints"]} == {"O-api-1", "O-api-2"}


def test_renewal_endpoint_accepts_window(client):
    narrow = client.get("/api/order/renewal?window_days=3", headers=CUST).json()

    assert narrow["window_days"] == 3
    assert narrow["due_count"] == 1
    assert narrow["upcoming_count"] == 0


def test_renewal_endpoint_rejects_bad_window(client):
    assert client.get("/api/order/renewal?window_days=400", headers=CUST).status_code == 422


def test_renewal_endpoint_requires_login(client):
    assert client.get("/api/order/renewal").status_code == 401


def test_expire_endpoint_archives_for_admin(client):
    response = client.post("/api/order/expire", headers=ADMIN)

    assert response.status_code == 200
    payload = response.json()
    assert payload["archived"] == ["O-api-1"]
    assert payload["has_more"] is False

    after = client.get("/api/order/renewal", headers=CUST).json()
    assert after["due_count"] == 1
    assert after["upcoming_count"] == 1


def test_expire_endpoint_rejects_customer(client):
    assert client.post("/api/order/expire", headers=CUST).status_code == 403


def test_expire_endpoint_rejects_invalid_limit(client):
    assert client.post("/api/order/expire?limit=0", headers=ADMIN).status_code == 422
