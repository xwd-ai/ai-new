"""订单接口层测试：八个订单接口的鉴权、契约与错误口径。

已有测试各自深入一条业务规则（服务范围、续费、管理列表、工具化），
本文件站在 HTTP 入口统一核对四件事，防止接口层在重构中出现漂移：

1. 鉴权矩阵：每个接口对匿名 / 普通用户 / 管理员分别是什么结果，
   哪个接口是匿名宽松（/order 返回空订单）而其余一律拒绝；
2. 字段契约：响应的键集合逐个冻结，多一个少一个都视为契约变更；
3. 错误口径：401 / 403 / 404 / 400 / 422 各自的状态码与错误体结构，
   业务异常走 {error, error_code, error_detail?}，参数校验走框架错误；
4. 跨接口一致：同一个订单在列表、范围判定、功能判定、续费与管理详情里
   给出的结论必须互不矛盾，归档后各接口同步收紧。

全部使用临时 SQLite，不读写真实的 app.db。
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
from backend.services import orders as orders_service

ADMIN = {"Authorization": "Bearer tk-admin-order-api"}
CUST_A = {"Authorization": "Bearer tk-cust-a-order-api"}
CUST_B = {"Authorization": "Bearer tk-cust-b-order-api"}
CUST_NONE = {"Authorization": "Bearer tk-cust-none-order-api"}

NOW = time.time()
DAY = 86400.0

# 订单接口清单：路径、方法、以及三种身份下的预期状态码。
# 只有 /order 是匿名宽松的 200，其余接口都必须先登录。
ENDPOINTS = [
    ("get", "/api/order", 200, 200, 200),
    ("get", "/api/order/list", 401, 200, 200),
    ("get", "/api/order/scope", 401, 200, 200),
    ("get", "/api/order/feature-scope", 401, 200, 200),
    ("get", "/api/order/renewal", 401, 200, 200),
    ("get", "/api/order/admin", 401, 403, 200),
    ("get", "/api/order/admin/O-A1", 401, 403, 200),
    ("post", "/api/order/expire", 401, 403, 200),
]

SCOPE_KEYS = {
    "has_order", "order_id", "tier", "tier_label", "order_status", "period",
    "features", "feature", "in_service", "allowed", "reason_code", "reason", "message",
}
PERIOD_KEYS = {
    "status", "status_label", "started_at", "expires_at", "started_text",
    "expires_text", "days_total", "days_used", "days_remaining", "percent_used",
    "active", "expired", "not_started",
}
FEATURE_KEYS = {"query", "code", "name", "included"}
LIST_KEYS = {
    "has_order", "order_count", "in_service_count", "primary_order_id",
    "orders", "features", "message",
}
LIST_ITEM_KEYS = {
    "order_id", "product_tier", "tier", "tier_label", "order_status",
    "period", "in_service", "allowed", "reason_code", "reason",
}
FEATURE_SCOPE_KEYS = {
    "has_order", "feature", "in_scope", "covered_by", "order_count",
    "in_service_count", "reason_code", "reason", "message", "orders",
}
RENEWAL_KEYS = {
    "has_order", "order_count", "remind_count", "due_count", "upcoming_count",
    "window_days", "hints", "message",
}
HINT_KEYS = {
    "order_id", "product_tier", "tier_label", "order_status", "state",
    "state_label", "remind", "urgency", "suggested_action", "window_days",
    "days_remaining", "days_overdue", "expires_text", "message",
}
ADMIN_LIST_KEYS = {
    "items", "total", "page", "page_size", "pages", "has_more", "order",
    "statuses", "tiers", "user_id", "keyword", "by_status", "by_tier", "message",
}
ADMIN_ITEM_KEYS = {
    "order_id", "user_id", "product_tier", "tier", "tier_label", "status",
    "status_label", "period", "in_service", "expires_text", "days_remaining",
}
ADMIN_DETAIL_KEYS = {
    "order_id", "user_id", "product_tier", "tier", "tier_label", "product_name",
    "status", "status_label", "period", "in_service", "expires_text",
    "days_remaining", "allowed", "reason_code", "reason", "features",
    "user_order_count", "status_flow", "renewal", "message",
}
STATUS_FLOW_KEYS = {
    "order_id", "status", "status_label", "known", "terminal",
    "allowed_statuses", "allowed_labels",
}
EXPIRE_KEYS = {"limit", "scanned", "remaining", "has_more", "archived", "skipped", "message"}


def _make_order(order_id, user_id, tier, status, start_offset, expire_offset):
    return Order(
        id=order_id,
        user_id=user_id,
        product_tier=tier,
        status=status,
        started_at=NOW + start_offset * DAY,
        expires_at=NOW + expire_offset * DAY,
    )


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    """临时库：三类账号（有过期订单 / 只有基础版 / 无订单）与管理员。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'order_api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, orders_service, auth_service):
        monkeypatch.setattr(module, "SessionLocal", factory)

    db = factory()
    try:
        db.add_all(
            [
                User(id="u-admin", username="admin", password_hash="x", name="客服小张", role="admin"),
                User(id="u-a", username="cust-a", password_hash="x", name="客户甲", role="customer"),
                User(id="u-b", username="cust-b", password_hash="x", name="客户乙", role="customer"),
                User(id="u-none", username="cust-none", password_hash="x", name="新客户", role="customer"),
                Session(token="tk-admin-order-api", user_id="u-admin", username="admin",
                        name="客服小张", role="admin", expires_at=NOW + 3600),
                Session(token="tk-cust-a-order-api", user_id="u-a", username="cust-a",
                        name="客户甲", role="customer", expires_at=NOW + 3600),
                Session(token="tk-cust-b-order-api", user_id="u-b", username="cust-b",
                        name="客户乙", role="customer", expires_at=NOW + 3600),
                Session(token="tk-cust-none-order-api", user_id="u-none", username="cust-none",
                        name="新客户", role="customer", expires_at=NOW + 3600),
                Product(id="basic", name="基础版", features=["基础导出", "单文件上传"]),
                Product(id="pro", name="专业版",
                        features=["基础导出", "单文件上传", "批量导出", "高级报表"]),
                # u-a：一条在期专业版 + 一条已过期的基础版
                _make_order("O-A1", "u-a", "pro", "active", -10, 20),
                _make_order("O-A2", "u-a", "basic", "expired", -90, -30),
                # u-b：一条长期在期的基础版，功能上不含批量导出
                _make_order("O-B1", "u-b", "basic", "active", -5, 100),
            ]
        )
        db.commit()
    finally:
        db.close()

    yield factory
    engine.dispose()


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(api_tickets.tickets_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


def _assert_error(response, status, code):
    """业务错误：状态码与错误码一致，响应体三键之内。"""
    assert response.status_code == status
    body = response.json()
    assert body["error_code"] == code
    assert set(body) <= {"error", "error_code", "error_detail"}
    assert body["error"]
    return body


# ---------------------------------------------------------------------------
# 鉴权矩阵：八个接口 × 三种身份
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,anon,customer,admin", ENDPOINTS)
def test_鉴权矩阵(client, method, path, anon, customer, admin):
    call = getattr(client, method)
    assert call(path).status_code == anon
    assert call(path, headers=CUST_A).status_code == customer
    assert call(path, headers=ADMIN).status_code == admin


def test_未登录的错误码统一为工单模块的未登录码(client):
    for method, path, _, _, _ in ENDPOINTS:
        response = getattr(client, method)(path)
        if response.status_code == 401:
            body = _assert_error(response, 401, "TICKET_UNAUTHORIZED")
            assert body["error"] == "未登录"
            # 401 不带补充上下文，错误体固定两键
            assert set(body) == {"error", "error_code"}


def test_普通用户访问管理接口的错误码为无权限(client):
    for method, path, _, customer, _ in ENDPOINTS:
        if customer != 403:
            continue
        response = getattr(client, method)(path, headers=CUST_A)
        body = _assert_error(response, 403, "TICKET_FORBIDDEN")
        assert set(body) == {"error", "error_code"}


def test_伪造令牌按未登录处理(client):
    headers = {"Authorization": "Bearer tk-not-a-real-session"}
    for method, path, _, _, _ in ENDPOINTS:
        if path == "/api/order":
            continue
        assert getattr(client, method)(path, headers=headers).status_code == 401


def test_只有订单总览对匿名宽松(client):
    body = client.get("/api/order").json()
    assert body == {"order": None}


# ---------------------------------------------------------------------------
# 字段契约：响应的键集合逐个冻结
# ---------------------------------------------------------------------------


def test_订单总览字段固定(client):
    body = client.get("/api/order", headers=CUST_A).json()
    assert set(body) == {"order"}
    order = body["order"]
    assert set(order) == {
        "has_order", "order_id", "product_tier", "product_name",
        "in_service", "features", "expires_at",
    }
    assert order["has_order"] is True
    assert order["order_id"] == "O-A1"
    assert order["product_tier"] == "pro"
    assert order["product_name"] == "专业版"
    assert order["in_service"] is True
    assert "批量导出" in order["features"]


def test_无订单账号的订单总览给空订单(client):
    """登录但没有订单：给一份 has_order=false 的结论，与匿名时的 null 区分。"""
    body = client.get("/api/order", headers=CUST_NONE).json()
    assert body["order"] == {"has_order": False, "message": "未查询到该用户的订单记录"}


def test_订单列表字段固定(client):
    body = client.get("/api/order/list", headers=CUST_A).json()
    assert set(body) == LIST_KEYS
    assert body["has_order"] is True
    assert body["order_count"] == 2
    assert body["in_service_count"] == 1
    assert body["primary_order_id"] == "O-A1"
    for item in body["orders"]:
        assert set(item) == LIST_ITEM_KEYS
        assert set(item["period"]) == PERIOD_KEYS
    assert set(body["features"]) == {"基础导出", "单文件上传", "批量导出", "高级报表"}


def test_无订单的列表给空视图(client):
    body = client.get("/api/order/list", headers=CUST_NONE).json()
    assert set(body) == LIST_KEYS
    assert body["has_order"] is False
    assert body["orders"] == [] and body["features"] == []
    assert body["primary_order_id"] is None and body["message"]


def test_服务范围字段固定(client):
    body = client.get("/api/order/scope", headers=CUST_A).json()
    assert set(body) == SCOPE_KEYS
    assert set(body["period"]) == PERIOD_KEYS
    # 没给功能时功能块为 null，不编一份空结论冒充判定结果
    assert body["feature"] is None
    assert body["has_order"] is True and body["order_id"] == "O-A1"
    assert body["tier"] == "pro" and body["tier_label"] == "专业版"
    assert body["in_service"] is True and body["allowed"] is True
    assert body["reason_code"] == "in_service" and body["message"]


def test_服务范围的功能判定随入参变化(client):
    hit = client.get("/api/order/scope", params={"feature": "批量导出"}, headers=CUST_A).json()
    assert set(hit["feature"]) == FEATURE_KEYS
    assert hit["feature"]["query"] == "批量导出"
    assert hit["feature"]["name"] == "批量导出"
    assert hit["allowed"] is True

    # 基础版不含批量导出：整体服务在期，但功能层不放行
    miss = client.get("/api/order/scope", params={"feature": "批量导出"}, headers=CUST_B).json()
    assert miss["in_service"] is True
    assert miss["feature"]["included"] is False
    assert miss["allowed"] is False
    assert miss["reason_code"] == "feature_not_included"


def test_功能判定字段固定(client):
    body = client.get("/api/order/feature-scope", params={"feature": "批量导出"},
                      headers=CUST_A).json()
    assert set(body) == FEATURE_SCOPE_KEYS
    assert set(body["feature"]) == FEATURE_KEYS
    assert body["in_scope"] is True
    assert body["covered_by"] == "O-A1"
    assert body["order_count"] == 2 and body["in_service_count"] == 1
    for item in body["orders"]:
        assert set(item) == {
            "order_id", "tier_label", "order_status", "in_service",
            "feature_included", "reason_code", "reason",
        }


def test_功能判定要求给出功能名(client):
    body = client.get("/api/order/feature-scope", headers=CUST_A).json()
    assert body["in_scope"] is False
    assert body["reason_code"] == "feature_required"
    assert body["feature"]["included"] is False


def test_续费提醒字段固定(client):
    body = client.get("/api/order/renewal", headers=CUST_A).json()
    assert set(body) == RENEWAL_KEYS
    assert body["window_days"] == 30
    assert body["order_count"] == 2
    assert body["remind_count"] == body["due_count"] + body["upcoming_count"]
    for hint in body["hints"]:
        assert set(hint) == HINT_KEYS


def test_管理列表字段固定(client):
    body = client.get("/api/order/admin", headers=ADMIN).json()
    assert set(body) == ADMIN_LIST_KEYS
    assert body["total"] == 3
    for item in body["items"]:
        assert set(item) == ADMIN_ITEM_KEYS
        assert set(item["period"]) == PERIOD_KEYS
    for group in body["by_status"] + body["by_tier"]:
        assert set(group) == {"key", "label", "count"}


def test_管理详情字段固定(client):
    body = client.get("/api/order/admin/O-A1", headers=ADMIN).json()
    assert set(body) == ADMIN_DETAIL_KEYS
    assert set(body["status_flow"]) == STATUS_FLOW_KEYS
    assert set(body["renewal"]) == HINT_KEYS
    assert body["order_id"] == "O-A1" and body["user_id"] == "u-a"
    assert body["product_name"] == "专业版"
    assert body["user_order_count"] == 2
    assert body["status_flow"]["status"] == "active"
    assert set(body["status_flow"]["allowed_statuses"]) == {"expired", "cancelled"}
    assert body["status_flow"]["terminal"] is False


def test_归档结果字段固定(client):
    body = client.post("/api/order/expire", headers=ADMIN).json()
    assert set(body) == EXPIRE_KEYS
    assert body["limit"] == 100
    assert isinstance(body["archived"], list) and isinstance(body["skipped"], list)


# ---------------------------------------------------------------------------
# 错误口径：入参、找不到、越界
# ---------------------------------------------------------------------------


def test_管理详情查不到订单返回404(client):
    body = _assert_error(client.get("/api/order/admin/O-9999", headers=ADMIN),
                         404, "ORDER_NOT_FOUND")
    assert body["error_detail"]["order_id"] == "O-9999"


def test_未知状态筛选返回400(client):
    body = _assert_error(client.get("/api/order/admin?status=unknown", headers=ADMIN),
                         400, "ORDER_INVALID_FILTER")
    assert body["error_detail"]["field"] == "status"


def test_未知版本筛选返回400(client):
    body = _assert_error(client.get("/api/order/admin?tier=enterprise", headers=ADMIN),
                         400, "ORDER_INVALID_FILTER")
    assert body["error_detail"]["field"] == "tier"


def test_非法排序返回400(client):
    _assert_error(client.get("/api/order/admin?order=created", headers=ADMIN),
                  400, "ORDER_INVALID_FILTER")


@pytest.mark.parametrize(
    "path",
    [
        "/api/order/admin?page=0",
        "/api/order/admin?page_size=0",
        "/api/order/admin?page_size=101",
        "/api/order/renewal?window_days=-1",
        "/api/order/renewal?window_days=366",
        "/api/order/scope?feature=" + "功" * 51,
        "/api/order/feature-scope?feature=" + "功" * 51,
    ],
)
def test_入参越界由框架拒绝(client, path):
    """长度与区间校验交给 FastAPI，业务层不必重复判断。"""
    assert client.get(path, headers=ADMIN).status_code == 422


def test_归档条数越界被拒绝(client):
    assert client.post("/api/order/expire?limit=0", headers=ADMIN).status_code == 422
    assert client.post("/api/order/expire?limit=501", headers=ADMIN).status_code == 422
    assert client.post("/api/order/expire?limit=500", headers=ADMIN).status_code == 200


def test_订单接口全部注册到路由表(client):
    """路径拼错时返回 404 而不是 405，说明方法也一并注册了。"""
    for method, path, _, _, _ in ENDPOINTS:
        concrete = path.replace("{order_id}", "O-A1")
        assert getattr(client, method)(concrete, headers=ADMIN).status_code != 405
    paths = client.get("/openapi.json").json()["paths"]
    assert {
        "/api/order", "/api/order/list", "/api/order/scope",
        "/api/order/feature-scope", "/api/order/renewal", "/api/order/admin",
        "/api/order/admin/{order_id}", "/api/order/expire",
    } <= set(paths)


# ---------------------------------------------------------------------------
# 跨接口一致：同一订单在各接口的结论互不矛盾
# ---------------------------------------------------------------------------


def test_列表主订单与服务范围指向同一条(client):
    listing = client.get("/api/order/list", headers=CUST_A).json()
    scope = client.get("/api/order/scope", headers=CUST_A).json()
    assert listing["primary_order_id"] == scope["order_id"]
    item = next(o for o in listing["orders"] if o["order_id"] == scope["order_id"])
    assert item["in_service"] == scope["in_service"]
    assert item["allowed"] == scope["allowed"]


def test_指定订单的判定与列表逐条一致(client):
    listing = client.get("/api/order/list", headers=CUST_A).json()
    by_id = {o["order_id"]: o for o in listing["orders"]}
    for order_id, item in by_id.items():
        scope = client.get("/api/order/scope", params={"order_id": order_id},
                           headers=CUST_A).json()
        assert scope["order_id"] == order_id
        assert scope["in_service"] == item["in_service"]
        assert scope["reason_code"] == item["reason_code"]


def test_功能判定覆盖项必须来自在期订单(client):
    body = client.get("/api/order/feature-scope", params={"feature": "批量导出"},
                      headers=CUST_A).json()
    active = {item["order_id"] for item in body["orders"] if item["in_service"]}
    assert body["covered_by"] in active
    assert body["in_service_count"] == len(active)


def test_单条判定与跨订单判定的差异(client, db_session):
    """主订单不含功能、另有一条在期订单含：单条判定拒绝，跨订单判定放行。

    这正是两个接口分开的原因 —— 只看主订单会把「另外一个版本能用」
    误判成不可用，多产品用户需要的是跨订单汇总的结论。
    """
    db = db_session()
    try:
        # u-b 的 O-B1 基础版到期更晚，会成为主订单；新加的 O-B2 才是专业版
        db.add(_make_order("O-B2", "u-b", "pro", "active", -3, 50))
        db.commit()
    finally:
        db.close()

    listing = client.get("/api/order/list", headers=CUST_B).json()
    assert listing["primary_order_id"] == "O-B1"

    single = client.get("/api/order/scope", params={"feature": "批量导出"},
                        headers=CUST_B).json()
    assert single["order_id"] == "O-B1"
    assert single["allowed"] is False
    assert single["reason_code"] == "feature_not_included"

    across = client.get("/api/order/feature-scope", params={"feature": "批量导出"},
                        headers=CUST_B).json()
    assert across["in_scope"] is True
    assert across["covered_by"] == "O-B2"
    assert across["order_count"] == 2 and across["in_service_count"] == 2


def test_续费提醒只覆盖有订单的账号(client):
    listing = client.get("/api/order/list", headers=CUST_A).json()
    renewal = client.get("/api/order/renewal", headers=CUST_A).json()
    assert renewal["order_count"] == listing["order_count"] == len(renewal["hints"])
    assert {h["order_id"] for h in renewal["hints"]} == {
        o["order_id"] for o in listing["orders"]
    }

    empty = client.get("/api/order/renewal", headers=CUST_NONE).json()
    assert empty["has_order"] is False and empty["hints"] == []
    assert empty["remind_count"] == 0


def test_管理详情与列表摘要对同一条订单结论一致(client):
    listing = client.get("/api/order/admin?keyword=O-A1", headers=ADMIN).json()
    assert listing["total"] == 1
    item = listing["items"][0]

    detail = client.get("/api/order/admin/O-A1", headers=ADMIN).json()
    for key in ("order_id", "user_id", "product_tier", "tier", "tier_label",
                "status", "status_label", "in_service", "expires_text", "days_remaining"):
        assert detail[key] == item[key]
    assert set(detail["period"]) == set(item["period"])
    assert detail["period"]["days_remaining"] == item["days_remaining"]
    # 详情多给的整单结论与 /order/scope 同口径
    scope = client.get("/api/order/scope", params={"order_id": "O-A1"},
                       headers=CUST_A).json()
    assert detail["allowed"] == scope["allowed"]
    assert detail["reason_code"] == scope["reason_code"]


def test_客户的订单只能看自己的(client):
    """用他人订单号查询按「没有这条订单」处理，不暴露归属。"""
    scope = client.get("/api/order/scope", params={"order_id": "O-B1"},
                       headers=CUST_A).json()
    assert scope["has_order"] is False
    assert scope["reason_code"] == "no_order"
    assert scope["order_id"] is None

    listing = client.get("/api/order/list", headers=CUST_A).json()
    assert "O-B1" not in {o["order_id"] for o in listing["orders"]}


def test_管理员自己的订单视图为空(client):
    assert client.get("/api/order/list", headers=ADMIN).json()["has_order"] is False
    assert client.get("/api/order/scope", headers=ADMIN).json()["reason_code"] == "no_order"
    assert client.get("/api/order/renewal", headers=ADMIN).json()["order_count"] == 0


# ---------------------------------------------------------------------------
# 归档链路：管理接口改动后各查询接口同步收紧
# ---------------------------------------------------------------------------


def test_归档到期订单后各接口结论同步收紧(client, db_session):
    db = db_session()
    try:
        db.add(_make_order("O-A3", "u-a", "pro", "active", -60, -1))
        db.commit()
    finally:
        db.close()

    before = client.get("/api/order/scope", params={"order_id": "O-A3"},
                        headers=CUST_A).json()
    assert before["order_status"] == "active"
    assert before["in_service"] is False
    assert before["reason_code"] == "expired"

    result = client.post("/api/order/expire", headers=ADMIN).json()
    assert "O-A3" in result["archived"]
    assert result["has_more"] is False

    detail = client.get("/api/order/admin/O-A3", headers=ADMIN).json()
    assert detail["status"] == "expired" and detail["status_label"] == "过期"
    assert detail["in_service"] is False
    assert detail["allowed"] is False
    assert detail["status_flow"]["allowed_statuses"] == ["active", "cancelled"]

    listing = client.get("/api/order/list", headers=CUST_A).json()
    archived = next(o for o in listing["orders"] if o["order_id"] == "O-A3")
    assert archived["order_status"] == "expired"
    assert archived["in_service"] is False
    assert listing["in_service_count"] == 1


def test_归档是幂等的(client):
    first = client.post("/api/order/expire", headers=ADMIN).json()
    second = client.post("/api/order/expire", headers=ADMIN).json()
    # 首次也没有候选订单：两条已过期的基础版不在归档范围内
    assert first["archived"] == []
    assert second["archived"] == []
    assert second["remaining"] == 0


def test_归档不会改动退订与在期订单(client, db_session):
    db = db_session()
    try:
        db.add(_make_order("O-A4", "u-a", "pro", "cancelled", -60, -1))
        db.commit()
    finally:
        db.close()

    client.post("/api/order/expire", headers=ADMIN)
    detail = client.get("/api/order/admin/O-A4", headers=ADMIN).json()
    assert detail["status"] == "cancelled"
