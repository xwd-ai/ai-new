"""订单服务范围的组合判定测试。

与 test_order_service_scope.py 的分工：那个文件逐项校验能力矩阵、服务期边界
与综合判定的单点行为；这里只问一件事——把版本、服务期、订单状态与功能四类
输入**组合起来**，结论是否符合预期，以及接口是否原样透出这个结论。

组合的期望值在用例里按业务规则独立算一遍，不复用被测函数的判断顺序，
否则函数改错时用例会跟着一起错。四类输入的期望优先级依次是：
版本不可识别 → 订单失效 → 服务期未开始 / 已到期 / 无法判定 → 功能不可识别 / 不在版本内。
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
    "expired": (NOW - 20 * DAY, NOW - 10 * DAY),
    "unknown": (NOW - 10 * DAY, NOW - 20 * DAY),  # 起止颠倒的数据问题
}

FEATURES = ("", "批量导出", "基础导出", "量子计算")


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


def _expected_code(tier: str, period: str, status: str, feature: str) -> str:
    """按业务规则独立推导期望原因码，作为被测实现的对照。"""
    if tier not in ("basic", "pro"):
        return "tier_unknown"
    if status != "active":
        return "order_inactive"
    if period != "active":
        return "period_unknown" if period == "unknown" else period
    if not feature:
        return "in_service"
    if feature == "量子计算":
        return "feature_unknown"
    included = BASIC_FEATURES if tier == "basic" else PRO_FEATURES
    return "in_service" if feature in included else "feature_not_included"


# ---------------------------------------------------------------------------
# 一、四类输入的组合矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["basic", "pro", "enterprise"])
@pytest.mark.parametrize("period", list(PERIODS))
@pytest.mark.parametrize("status", ["active", "cancelled"])
@pytest.mark.parametrize("feature", FEATURES)
def test_版本服务期状态功能的组合结论(tier, period, status, feature):
    """96 种组合逐个对照期望原因码，并校验三处派生结论自洽。"""
    scope = orders.evaluate_service_scope(
        _order(tier, period, status), feature=feature or None, now=NOW
    )
    expected = _expected_code(tier, period, status, feature)

    assert scope["reason_code"] == expected
    assert scope["reason"], "每种结论都要有可展示的说明"
    # 放行要求版本可识别、服务在期内、功能在版本内三条同时成立
    assert scope["allowed"] == (expected == "in_service")
    assert scope["allowed"] == (
        scope["known_tier"] and scope["in_service"] and scope["feature_included"]
    )
    # in_service 只由服务期与订单状态决定，与功能入参无关
    assert scope["in_service"] == (period == "active" and status == "active")


def test_版本不可识别时先报版本而不是其它问题():
    """版本、服务期、状态同时异常时，结论落在最上游的版本问题上。"""
    scope = orders.evaluate_service_scope(
        _order("enterprise", "expired", "cancelled"), feature="批量导出", now=NOW
    )
    assert scope["reason_code"] == "tier_unknown"
    assert scope["known_tier"] is False
    assert scope["feature_included"] is False


def test_服务期已到期先于功能不在版本内():
    """基础版用户问批量导出且已到期，结论应是已到期。

    先报「功能不包含」会让用户以为升级版本就能解决，掩盖了真正的前置问题。
    """
    scope = orders.evaluate_service_scope(
        _order("basic", "expired", "active"), feature="批量导出", now=NOW
    )
    assert scope["reason_code"] == "expired"


def test_未开始的服务期不算服务中():
    """服务期尚未开始的订单，in_service 必须为 false。"""
    scope = orders.evaluate_service_scope(_order(period="not_started"), now=NOW)
    assert scope["in_service"] is False
    assert scope["period"]["not_started"] is True
    assert scope["reason_code"] == "not_started"


def test_功能入参留空时只看整体状态():
    """不给功能时 feature 为 None、feature_included 视为通过，但仍要服务在期内才放行。"""
    active = orders.evaluate_service_scope(_order(), now=NOW)
    assert active["feature"] is None and active["feature_included"] is True
    assert active["allowed"] is True

    expired = orders.evaluate_service_scope(_order(period="expired"), now=NOW)
    assert expired["feature"] is None and expired["feature_included"] is True
    assert expired["allowed"] is False


def test_功能原文按空白归一后回显():
    """功能入参带多余空白时，判定用归一后的文本，回显也用同一份。"""
    scope = orders.evaluate_service_scope(_order(), feature="  批量   导出 ", now=NOW)
    assert scope["feature"]["query"] == "批量 导出"
    assert scope["reason_code"] == "in_service"


def test_无订单时其余字段保持空值():
    """没有订单记录时不应凭空造出版本与服务期。"""
    scope = orders.evaluate_service_scope(None, feature="批量导出", now=NOW)
    assert scope["has_order"] is False
    assert scope["period"] is None
    assert scope["features"] == []
    assert scope["reason_code"] == "no_order"
    assert scope["allowed"] is False


# ---------------------------------------------------------------------------
# 二、查询结论与判定结果一致
# ---------------------------------------------------------------------------


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 两个产品配置行，服务层取数指向它。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'order-scope.db'}",
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


def _seed(factory, user_id: str, tier="pro", period="active", status="active") -> dict:
    started_at, expires_at = PERIODS[period]
    db = factory()
    try:
        db.add(
            Order(
                id=f"O-{user_id}",
                user_id=user_id,
                product_tier=tier,
                status=status,
                started_at=started_at,
                expires_at=expires_at,
            )
        )
        db.commit()
    finally:
        db.close()
    return {
        "id": f"O-{user_id}",
        "user_id": user_id,
        "product_tier": tier,
        "status": status,
        "started_at": started_at,
        "expires_at": expires_at,
    }


def test_查询结论与纯函数判定逐字段一致(factory):
    """describe_order 只负责取数，结论必须与纯函数判定完全一致。"""
    order = _seed(factory, "u-pro")
    expected = orders.evaluate_service_scope(order, feature="批量导出", now=NOW)
    actual = orders.describe_order("u-pro", feature="批量导出", now=NOW)

    for key in ("has_order", "order_id", "tier", "tier_label", "order_status",
                "period", "features", "feature", "in_service", "allowed",
                "reason_code", "reason"):
        assert actual[key] == expected[key], key


def test_查询结论的说明文案(factory):
    """可展示文案随结论变化：服务中带到期与剩余天数，异常时直接给原因。"""
    _seed(factory, "u-pro")
    _seed(factory, "u-basic", tier="basic")
    _seed(factory, "u-expired", period="expired")

    active = orders.describe_order("u-pro", now=NOW)["message"]
    assert active.startswith("专业版服务中")
    assert "剩余 10 天" in active

    with_feature = orders.describe_order("u-pro", feature="批量导出", now=NOW)["message"]
    assert with_feature.startswith("专业版包含「批量导出」")

    not_included = orders.describe_order("u-basic", feature="批量导出", now=NOW)["message"]
    assert "不包含" in not_included

    expired = orders.describe_order("u-expired", now=NOW)
    assert "服务期已结束" in expired["message"]
    assert expired["period"]["expires_text"] in expired["message"]
    assert orders.describe_order("u-none", now=NOW)["message"] == (
        "未查询到订单记录，无法判断服务范围"
    )


def test_查询结论字段集合固定(factory):
    """结论字段与响应契约对齐，新增字段需同步改契约与用例。"""
    _seed(factory, "u-pro")
    payload = orders.describe_order("u-pro", feature="批量导出", now=NOW)
    assert set(payload) == {
        "has_order",
        "order_id",
        "tier",
        "tier_label",
        "order_status",
        "period",
        "features",
        "feature",
        "in_service",
        "allowed",
        "reason_code",
        "reason",
        "message",
    }
    assert set(payload["feature"]) == {"query", "code", "name", "included"}
    assert set(payload["period"]) == {
        "status",
        "status_label",
        "started_at",
        "expires_at",
        "started_text",
        "expires_text",
        "days_total",
        "days_used",
        "days_remaining",
        "percent_used",
        "active",
        "expired",
        "not_started",
    }


def test_旧订单接口结论与新判定一致(factory):
    """query_order 的 7 键契约不变，in_service 与 describe_order 同源。"""
    _seed(factory, "u-pro", period="not_started")
    legacy = orders.query_order("u-pro")
    detail = orders.describe_order("u-pro", now=time.time())

    assert set(legacy) == {"has_order", "order_id", "product_tier", "product_name",
                           "in_service", "features", "expires_at"}
    assert legacy["in_service"] is False
    assert legacy["in_service"] == detail["in_service"]


# ---------------------------------------------------------------------------
# 三、接口层
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 六类订单形态的账号，挂载工单路由（订单接口在本路由内）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'order-api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, auth_service, orders):
        monkeypatch.setattr(module, "SessionLocal", session_factory)

    now = time.time()
    users = [
        ("u-admin", "admin", "admin", "客服小张"),
        ("u-pro", "pro", "customer", "客户甲"),
        ("u-basic", "basic", "customer", "客户乙"),
        ("u-expired", "expired", "customer", "客户丙"),
        ("u-notstart", "notstart", "customer", "客户丁"),
        ("u-cancel", "cancel", "customer", "客户戊"),
        ("u-none", "none", "customer", "客户己"),
    ]
    db = session_factory()
    try:
        for user_id, key, role, name in users:
            db.add(User(id=user_id, username=f"scope-{key}", password_hash="x",
                        name=name, role=role))
            db.add(Session(token=f"tk-scope-{key}", user_id=user_id,
                           username=f"scope-{key}", name=name, role=role,
                           expires_at=now + 3600))
        db.add_all(
            [
                Product(id="basic", name="基础版", features=list(BASIC_FEATURES)),
                Product(id="pro", name="专业版", features=list(PRO_FEATURES)),
                Order(id="O-pro", user_id="u-pro", product_tier="pro", status="active",
                      started_at=now - 10 * DAY, expires_at=now + 10 * DAY),
                Order(id="O-basic", user_id="u-basic", product_tier="basic", status="active",
                      started_at=now - 10 * DAY, expires_at=now + 10 * DAY),
                Order(id="O-expired", user_id="u-expired", product_tier="pro", status="active",
                      started_at=now - 20 * DAY, expires_at=now - 10 * DAY),
                Order(id="O-notstart", user_id="u-notstart", product_tier="pro", status="active",
                      started_at=now + 10 * DAY, expires_at=now + 100 * DAY),
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
    return {"Authorization": f"Bearer tk-scope-{key}"}


def test_未登录取不到服务范围(client):
    """订单权益相关的结论不对匿名开放，错误体沿用工单模块的错误码。"""
    response = client.get("/api/order/scope")
    assert response.status_code == 401
    assert response.json() == {"error": "未登录", "error_code": "TICKET_UNAUTHORIZED"}


def test_服务中订单返回完整结论(client):
    response = client.get("/api/order/scope", headers=_headers("pro"))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "has_order", "order_id", "tier", "tier_label", "order_status", "period",
        "features", "feature", "in_service", "allowed", "reason_code", "reason", "message",
    }
    assert body["has_order"] is True
    assert (body["tier"], body["tier_label"]) == ("pro", "专业版")
    assert body["order_status"] == "active"
    assert body["period"]["status"] == "active"
    assert body["period"]["days_remaining"] == 10
    assert body["feature"] is None
    assert body["allowed"] is True and body["reason_code"] == "in_service"
    assert "服务期至" in body["message"]


@pytest.mark.parametrize(
    "key, feature, expected",
    [
        ("pro", "批量导出", "in_service"),
        ("pro", "基础导出", "in_service"),
        ("basic", "批量导出", "feature_not_included"),
        ("basic", "导出", "in_service"),
        ("pro", "量子计算", "feature_unknown"),
        ("expired", "批量导出", "expired"),
        ("notstart", "批量导出", "not_started"),
        ("cancel", "批量导出", "order_inactive"),
    ],
)
def test_按账号形态与功能给出对应结论(client, key, feature, expected):
    """一张覆盖版本差异、服务期三态、订单失效与功能解析的表。"""
    response = client.get(
        "/api/order/scope", params={"feature": feature}, headers=_headers(key)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reason_code"] == expected
    assert body["reason"], "每种结论都要有说明"
    if expected == "in_service":
        assert body["allowed"] is True and body["feature"]["included"] is True
    else:
        assert body["allowed"] is False


def test_无订单账号返回空结论(client):
    """没有订单时字段齐全但为空值，前端不必区分缺字段与缺订单。"""
    body = client.get("/api/order/scope", headers=_headers("none")).json()
    assert body["has_order"] is False
    assert body["order_id"] is None and body["period"] is None
    assert body["features"] == [] and body["feature"] is None
    assert body["allowed"] is False
    assert body["reason_code"] == "no_order"


def test_功能参数超长被拒绝(client):
    """功能名限长 50，超长走请求模型校验，返回 FastAPI 的 detail 列表。"""
    response = client.get(
        "/api/order/scope", params={"feature": "导" * 51}, headers=_headers("pro")
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_功能参数带空白仍能命中(client):
    """查询参数里的多余空白不影响判定，回显的是归一后的文本。"""
    body = client.get(
        "/api/order/scope", params={"feature": "  批量导出  "}, headers=_headers("pro")
    ).json()
    assert body["feature"]["query"] == "批量导出"
    assert body["feature"]["name"] == "批量导出"
    assert body["feature"]["included"] is True


def test_管理员查询自己的订单(client):
    """接口只查当前登录人，不因管理员身份扩大到他人订单。"""
    body = client.get("/api/order/scope", headers=_headers("admin")).json()
    assert body["has_order"] is False
