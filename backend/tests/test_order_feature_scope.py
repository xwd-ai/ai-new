"""功能服务范围判定测试。

与 test_order_query_scope.py 的分工：那个文件判定单条订单（主订单或指定订单）
「这条订单能不能用该功能」；这里判定跨全部订单的汇总结论——多产品用户里
主订单不覆盖某功能、但另一条在期订单覆盖时，必须判为在范围内。

期望值在用例里按业务规则独立推导：任一在期订单的版本包含该功能即在范围内；
不在范围内时按「没有订单 → 未给功能 → 功能不可识别 → 在期版本不包含 →
没有在期订单（报服务态原因）」的优先级给单一原因码。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api import tickets as api_tickets
from backend.database import Base
from backend.models import Order, Product, Session, User
from backend.services import auth as auth_service
from backend.services import orders

DAY = 86400
NOW = 1_760_000_000.0  # 固定时刻，避免用例随真实时钟波动

BASIC_FEATURES = ("基础导出", "单文件上传", "社区支持")
PRO_FEATURES = BASIC_FEATURES + ("批量导出", "高级报表", "优先客服支持", "API 接口")

PERIODS = {
    "not_started": (NOW + 10 * DAY, NOW + 100 * DAY),
    "active": (NOW - 10 * DAY, NOW + 10 * DAY),
    "active_late": (NOW - 10 * DAY, NOW + 50 * DAY),  # 到期更晚，用于排序用例
    "expired": (NOW - 20 * DAY, NOW - 10 * DAY),
}


def _order(tier="pro", period="active", status="active", order_id="O-1") -> dict:
    started_at, expires_at = PERIODS[period]
    return {
        "id": order_id,
        "user_id": "u-1",
        "product_tier": tier,
        "status": status,
        "started_at": started_at,
        "expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# 一、跨订单的汇总判定
# ---------------------------------------------------------------------------


def test_在期订单覆盖功能判为在范围内():
    verdict = orders.evaluate_feature_scope([_order()], feature="批量导出", now=NOW)
    assert verdict["in_scope"] is True
    assert verdict["covered_by"] == "O-1"
    assert verdict["reason_code"] == "in_scope"
    assert verdict["feature"]["code"] == "export.batch"
    assert verdict["feature"]["included"] is True
    assert "「批量导出」在服务范围内" in verdict["message"]
    assert "剩余 10 天" in verdict["message"]


def test_主订单不覆盖时仍按其它在期订单放行():
    """主订单（到期更晚的基础版）不含批量导出，但另一条在期专业版覆盖它。"""
    rows = [
        _order("basic", "active_late", order_id="O-basic"),
        _order("pro", "active", order_id="O-pro"),
    ]
    verdict = orders.evaluate_feature_scope(rows, feature="批量导出", now=NOW)
    assert verdict["in_scope"] is True
    assert verdict["covered_by"] == "O-pro"
    assert verdict["in_service_count"] == 2


def test_在期版本都不包含时报功能不包含():
    rows = [
        _order("basic", "active", order_id="O-basic"),
        _order("pro", "expired", order_id="O-pro"),
    ]
    verdict = orders.evaluate_feature_scope(rows, feature="批量导出", now=NOW)
    assert verdict["in_scope"] is False
    assert verdict["covered_by"] is None
    assert verdict["reason_code"] == "feature_not_included"
    assert "不包含" in verdict["reason"]
    # 版本层结论照常给出：功能属于专业版能力，只是那条订单不在期内
    assert verdict["feature"]["included"] is True


def test_没有在期订单时按主订单报服务态原因():
    cases = {
        "expired": "expired",
        "not_started": "not_started",
    }
    for period, expected in cases.items():
        verdict = orders.evaluate_feature_scope(
            [_order("pro", period, order_id="O-x")], feature="批量导出", now=NOW
        )
        assert verdict["reason_code"] == expected, period
        assert verdict["in_scope"] is False

    cancelled = orders.evaluate_feature_scope(
        [_order("pro", "active", "cancelled", order_id="O-c")],
        feature="批量导出",
        now=NOW,
    )
    assert cancelled["reason_code"] == "order_inactive"


def test_服务态原因先于功能不包含():
    """基础版已到期问批量导出：先报到期，升级版本解决不了没续费的问题。"""
    verdict = orders.evaluate_feature_scope(
        [_order("basic", "expired", order_id="O-basic")], feature="批量导出", now=NOW
    )
    assert verdict["reason_code"] == "expired"


def test_在期订单版本不可识别时报版本问题():
    verdict = orders.evaluate_feature_scope(
        [_order("enterprise", "active", order_id="O-x")], feature="批量导出", now=NOW
    )
    assert verdict["reason_code"] == "tier_unknown"
    assert verdict["feature"]["included"] is False


def test_功能不可识别时回显原文():
    verdict = orders.evaluate_feature_scope([_order()], feature="  量子计算 ", now=NOW)
    assert verdict["reason_code"] == "feature_unknown"
    assert verdict["feature"]["query"] == "量子计算"
    assert verdict["feature"]["included"] is False
    assert "量子计算" in verdict["reason"]


def test_未给功能时要求补充():
    verdict = orders.evaluate_feature_scope([_order()], feature="  ", now=NOW)
    assert verdict["reason_code"] == "feature_required"
    assert verdict["in_scope"] is False
    assert verdict["feature"] == {"query": "", "code": "", "name": "", "included": False}


def test_无订单时给空结论():
    verdict = orders.evaluate_feature_scope([], feature="批量导出", now=NOW)
    assert verdict["has_order"] is False
    assert verdict["order_count"] == 0
    assert verdict["orders"] == []
    assert verdict["reason_code"] == "no_order"
    assert verdict["in_scope"] is False


def test_逐单明细与计数():
    rows = [
        _order("basic", "active", order_id="O-basic"),
        _order("pro", "expired", order_id="O-pro"),
        _order("pro", "active", "cancelled", order_id="O-c"),
    ]
    verdict = orders.evaluate_feature_scope(rows, feature="批量导出", now=NOW)
    assert verdict["order_count"] == 3
    assert verdict["in_service_count"] == 1
    assert [item["order_id"] for item in verdict["orders"]] == [
        "O-basic", "O-pro", "O-c",
    ]
    by_id = {item["order_id"]: item for item in verdict["orders"]}
    assert by_id["O-pro"]["feature_included"] is True
    assert by_id["O-pro"]["in_service"] is False
    assert by_id["O-c"]["reason_code"] == "order_inactive"
    assert all(item["reason"] for item in verdict["orders"])


def test_判定结论字段集合固定():
    verdict = orders.evaluate_feature_scope([_order()], feature="批量导出", now=NOW)
    assert set(verdict) == {
        "has_order",
        "feature",
        "in_scope",
        "covered_by",
        "order_count",
        "in_service_count",
        "reason_code",
        "reason",
        "message",
        "orders",
    }
    assert set(verdict["feature"]) == {"query", "code", "name", "included"}
    assert set(verdict["orders"][0]) == {
        "order_id",
        "tier_label",
        "order_status",
        "in_service",
        "feature_included",
        "reason_code",
        "reason",
    }


# ---------------------------------------------------------------------------
# 二、取数后判定与纯函数一致
# ---------------------------------------------------------------------------


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 产品配置行，服务层取数指向它。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feature-scope.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(orders, "SessionLocal", session_factory)

    db = session_factory()
    try:
        db.add_all(
            [
                Product(id="basic", name="基础版", features=list(BASIC_FEATURES)),
                Product(id="pro", name="专业版", features=list(PRO_FEATURES)),
            ]
        )
        db.commit()
    finally:
        db.close()

    yield session_factory
    engine.dispose()


def _seed(factory, user_id: str, order_id: str, tier="pro", period="active", status="active") -> dict:
    started_at, expires_at = PERIODS[period]
    row = {
        "id": order_id,
        "user_id": user_id,
        "product_tier": tier,
        "status": status,
        "started_at": started_at,
        "expires_at": expires_at,
    }
    db = factory()
    try:
        db.add(Order(**row))
        db.commit()
    finally:
        db.close()
    return row


def test_查询结论与纯函数判定逐字段一致(factory):
    _seed(factory, "u-mix", "O-basic", tier="basic", period="active_late")
    _seed(factory, "u-mix", "O-pro", tier="pro", period="active")
    expected = orders.evaluate_feature_scope(
        orders.list_orders("u-mix"), feature="批量导出", now=NOW
    )
    actual = orders.describe_feature_scope("u-mix", feature="批量导出", now=NOW)
    assert actual == expected
    assert actual["covered_by"] == "O-pro"


# ---------------------------------------------------------------------------
# 三、接口层
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 多类订单形态的账号，挂载工单路由（订单接口在本路由内）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feature-api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, auth_service, orders):
        monkeypatch.setattr(module, "SessionLocal", session_factory)

    now = time.time()
    users = [
        ("u-pro", "pro", "customer", "客户甲"),
        ("u-basic", "basic", "customer", "客户乙"),
        ("u-mix", "mix", "customer", "客户丙"),
        ("u-expired", "expired", "customer", "客户丁"),
        ("u-cancel", "cancel", "customer", "客户戊"),
        ("u-none", "none", "customer", "客户己"),
    ]
    db = session_factory()
    try:
        for user_id, key, role, name in users:
            db.add(User(id=user_id, username=f"feat-{key}", password_hash="x",
                        name=name, role=role))
            db.add(Session(token=f"tk-feat-{key}", user_id=user_id,
                           username=f"feat-{key}", name=name, role=role,
                           expires_at=now + 3600))
        db.add_all(
            [
                Product(id="basic", name="基础版", features=list(BASIC_FEATURES)),
                Product(id="pro", name="专业版", features=list(PRO_FEATURES)),
                Order(id="O-pro", user_id="u-pro", product_tier="pro", status="active",
                      started_at=now - 10 * DAY, expires_at=now + 10 * DAY),
                Order(id="O-basic", user_id="u-basic", product_tier="basic", status="active",
                      started_at=now - 10 * DAY, expires_at=now + 10 * DAY),
                Order(id="O-mix-basic", user_id="u-mix", product_tier="basic", status="active",
                      started_at=now - 10 * DAY, expires_at=now + 50 * DAY),
                Order(id="O-mix-pro", user_id="u-mix", product_tier="pro", status="active",
                      started_at=now - 10 * DAY, expires_at=now + 10 * DAY),
                Order(id="O-expired", user_id="u-expired", product_tier="pro", status="active",
                      started_at=now - 20 * DAY, expires_at=now - 10 * DAY),
                Order(id="O-cancel", user_id="u-cancel", product_tier="pro", status="cancelled",
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


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer tk-feat-{key}"}


def test_未登录取不到功能判定(client):
    response = client.get("/api/order/feature-scope")
    assert response.status_code == 401
    assert response.json() == {"error": "未登录", "error_code": "TICKET_UNAUTHORIZED"}


@pytest.mark.parametrize(
    "key, feature, expected, in_scope",
    [
        ("pro", "批量导出", "in_scope", True),
        ("pro", "api", "in_scope", True),
        ("basic", "批量导出", "feature_not_included", False),
        ("mix", "批量导出", "in_scope", True),
        ("expired", "批量导出", "expired", False),
        ("cancel", "批量导出", "order_inactive", False),
        ("none", "批量导出", "no_order", False),
        ("pro", "量子计算", "feature_unknown", False),
    ],
)
def test_按账号形态与功能给出对应结论(client, key, feature, expected, in_scope):
    body = client.get(
        "/api/order/feature-scope", params={"feature": feature}, headers=_headers(key)
    ).json()
    assert body["reason_code"] == expected
    assert body["in_scope"] is in_scope
    assert body["reason"], "每种结论都要有说明"


def test_多订单用户由非主订单覆盖(client):
    """u-mix 的主订单是到期更晚的基础版，批量导出应判给在期的专业版。"""
    body = client.get(
        "/api/order/feature-scope", params={"feature": "批量导出"}, headers=_headers("mix")
    ).json()
    assert body["covered_by"] == "O-mix-pro"
    assert body["order_count"] == 2 and body["in_service_count"] == 2


def test_接口响应字段集合固定(client):
    body = client.get(
        "/api/order/feature-scope", params={"feature": "批量导出"}, headers=_headers("pro")
    ).json()
    assert set(body) == {
        "has_order", "feature", "in_scope", "covered_by", "order_count",
        "in_service_count", "reason_code", "reason", "message", "orders",
    }
    assert set(body["feature"]) == {"query", "code", "name", "included"}
    assert set(body["orders"][0]) == {
        "order_id", "tier_label", "order_status", "in_service",
        "feature_included", "reason_code", "reason",
    }


def test_功能参数超长被拒绝(client):
    response = client.get(
        "/api/order/feature-scope", params={"feature": "导" * 51}, headers=_headers("pro")
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
