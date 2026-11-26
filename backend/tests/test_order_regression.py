"""订单模块回归测试。

各功能自己的用例已经把单条规则抠得很细（服务范围、续费、管理列表、工具化），
回归测试盯的是「跨功能的共同约定」——任何一处改动破坏了下面六条，都会在这里暴露：

1. 只读接口不落库：七个查询接口被任意身份调用后，订单表与会话表必须原样不动；
2. 接口层只透传：同一份结论从服务层和 HTTP 入口取回来必须逐字段相等，
   接口层不得二次加工，服务层也不得偷偷少给字段；
3. 跨接口闭环：主订单、逐条判定、功能并集、续费提示、管理详情对同一条订单
   给出的版本、服务期与结论必须互相能对上；
4. 状态变化同步收敛：归档后各接口一起收紧、重复归档幂等、退订是终态不复活；
5. 数据隔离：客户之间看不到对方的订单号，管理接口只对管理员开放；
6. 排序分页与畸形入参：翻页不重不漏、分组计数覆盖全量、异常入参不产生 5xx。

全部用例使用独立临时 SQLite，不读写真实的 app.db。
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import tickets as api_tickets
from backend.database import Base
from backend.models import Order, Product, Session, Ticket, User
from backend.models.order import OrderStatus, OrderStatusTransitionError
from backend.services import auth as auth_service
from backend.services import orders as orders_service

NOW = time.time()
DAY = 86400.0

ADMIN = {"Authorization": "Bearer tk-ordreg-admin"}
MULTI = {"Authorization": "Bearer tk-ordreg-multi"}
EXPIRED = {"Authorization": "Bearer tk-ordreg-expired"}
SPREAD = {"Authorization": "Bearer tk-ordreg-spread"}
NONE = {"Authorization": "Bearer tk-ordreg-none"}

# 三个账号的订单组合刻意覆盖不同形态：双在期、全到期、退订与同到期时间并列。
# 一个账号看不到的形态，另一个账号能看到，跨接口口径才有得比。
ORDER_SEEDS = [
    # 双在期：专业版先到期，基础版后到期 —— 主订单是基础版，
    # 于是「主订单不含批量导出」与「跨订单含批量导出」会同时成立。
    ("O-M1", "u-multi", "pro", "active", -10, 20),
    ("O-M2", "u-multi", "basic", "active", -5, 90),
    # 一条已归档过期、一条状态仍有效但服务期已结束，后者是归档的唯一候选。
    ("O-E1", "u-expired", "pro", "expired", -90, -5),
    ("O-E2", "u-expired", "basic", "active", -100, -1),
    # 同到期时间的两条订单，用来验证排序的同值兜底与翻页稳定。
    ("O-S1", "u-spread", "basic", "active", -20, 60),
    ("O-S2", "u-spread", "pro", "cancelled", -60, 40),
    ("O-S3", "u-spread", "basic", "active", -30, 60),
]


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
def store(tmp_path, monkeypatch):
    """临时库：四个客户（双在期 / 全到期 / 分散订单 / 无订单）与一个管理员。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'order_regression.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, orders_service, auth_service):
        monkeypatch.setattr(module, "SessionLocal", factory)

    db = factory()
    try:
        db.add_all(
            [
                User(id="u-admin", username="ordreg-admin", password_hash="x",
                     name="客服小张", role="admin"),
                User(id="u-multi", username="ordreg-multi", password_hash="x",
                     name="客户甲", role="customer"),
                User(id="u-expired", username="ordreg-expired", password_hash="x",
                     name="客户乙", role="customer"),
                User(id="u-spread", username="ordreg-spread", password_hash="x",
                     name="客户丙", role="customer"),
                User(id="u-none", username="ordreg-none", password_hash="x",
                     name="新客户", role="customer"),
                Session(token="tk-ordreg-admin", user_id="u-admin", username="ordreg-admin",
                        name="客服小张", role="admin", expires_at=NOW + 3600),
                Session(token="tk-ordreg-multi", user_id="u-multi", username="ordreg-multi",
                        name="客户甲", role="customer", expires_at=NOW + 3600),
                Session(token="tk-ordreg-expired", user_id="u-expired", username="ordreg-expired",
                        name="客户乙", role="customer", expires_at=NOW + 3600),
                Session(token="tk-ordreg-spread", user_id="u-spread", username="ordreg-spread",
                        name="客户丙", role="customer", expires_at=NOW + 3600),
                Session(token="tk-ordreg-none", user_id="u-none", username="ordreg-none",
                        name="新客户", role="customer", expires_at=NOW + 3600),
                Product(id="basic", name="基础版", features=["基础导出", "单文件上传"]),
                Product(id="pro", name="专业版",
                        features=["基础导出", "单文件上传", "批量导出", "高级报表"]),
            ]
        )
        db.add_all([_make_order(*seed) for seed in ORDER_SEEDS])
        db.commit()
    finally:
        db.close()

    yield factory
    engine.dispose()


@pytest.fixture()
def client(store):
    app = FastAPI()
    app.include_router(api_tickets.tickets_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


def _snapshot(factory):
    """库状态快照：订单行字段 + 工单/会话行数。只读接口调用前后必须完全一致。"""
    db = factory()
    try:
        rows = db.query(Order).order_by(Order.id).all()
        orders = {
            row.id: (row.user_id, row.product_tier, row.status, row.started_at, row.expires_at)
            for row in rows
        }
        return orders, db.query(Ticket).count(), db.query(Session).count()
    finally:
        db.close()


def _status_of(factory, order_id):
    db = factory()
    try:
        return db.get(Order, order_id).status
    finally:
        db.close()


def _assert_same_as_service(body, expected, label=""):
    """响应对服务层结论：服务层给了的字段响应必须有，且值相等。

    反向不要求相等：响应模型会裁掉服务层的内部字段，这是允许的；
    但服务层给出的对外字段一个都不能少，否则前端会拿到缺字段的对象。
    """
    missing = set(expected) - set(body)
    assert not missing, f"{label} 响应缺少服务层字段 {sorted(missing)}"
    for key, value in expected.items():
        assert body[key] == value, f"{label} 字段 {key} 与服务层结论不一致"


# ---------------------------------------------------------------------------
# 一、只读接口不落库
# ---------------------------------------------------------------------------

READ_CALLS = [
    ("/api/order", MULTI),
    ("/api/order", {}),
    ("/api/order/list", MULTI),
    ("/api/order/list", EXPIRED),
    ("/api/order/list", NONE),
    ("/api/order/scope", MULTI),
    ("/api/order/scope?feature=批量导出", MULTI),
    ("/api/order/scope?order_id=O-M1", MULTI),
    ("/api/order/feature-scope?feature=批量导出", MULTI),
    ("/api/order/renewal", MULTI),
    ("/api/order/renewal?window_days=180", EXPIRED),
    ("/api/order/admin", ADMIN),
    ("/api/order/admin?page=2&page_size=2", ADMIN),
    ("/api/order/admin/O-M1", ADMIN),
]


@pytest.mark.parametrize("path,headers", READ_CALLS)
def test_只读接口不写库(client, store, path, headers):
    """查询订单不能有副作用：不归档、不改状态、不顺手建会话。"""
    before = _snapshot(store)
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert _snapshot(store) == before, f"{path} 读取后数据库发生了变化"


# ---------------------------------------------------------------------------
# 二、接口层只透传服务层结论
# ---------------------------------------------------------------------------

SERVICE_CALLS = [
    ("/api/order", MULTI, lambda: orders_service.query_order("u-multi"), "order"),
    ("/api/order/list", MULTI, lambda: orders_service.query_orders("u-multi"), None),
    ("/api/order/scope", MULTI, lambda: orders_service.describe_order("u-multi"), None),
    ("/api/order/scope?feature=批量导出", MULTI,
     lambda: orders_service.describe_order("u-multi", feature="批量导出"), None),
    ("/api/order/scope?order_id=O-M1", MULTI,
     lambda: orders_service.describe_order("u-multi", order_id="O-M1"), None),
    ("/api/order/feature-scope?feature=批量导出", MULTI,
     lambda: orders_service.describe_feature_scope("u-multi", "批量导出"), None),
    ("/api/order/renewal", MULTI, lambda: orders_service.list_renewal_hints("u-multi"), None),
    ("/api/order/admin", ADMIN, lambda: orders_service.list_all_orders(), None),
    ("/api/order/admin/O-M1", ADMIN,
     lambda: orders_service.describe_order_detail("O-M1"), None),
]


@pytest.mark.parametrize("path,headers,call,wrapper", SERVICE_CALLS)
def test_接口层与服务层同源(client, path, headers, call, wrapper):
    """同一份结论从两个入口取回来必须一致，防止接口层悄悄改口径。"""
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    if wrapper:
        body = body[wrapper]
    _assert_same_as_service(body, call(), path)


# ---------------------------------------------------------------------------
# 三、跨接口闭环：主订单、逐条判定、功能并集、续费、管理详情
# ---------------------------------------------------------------------------


def test_主订单在各接口指向同一条订单(client):
    """/order、/order/list、/order/scope 对「在用哪条订单」只能有一个答案。"""
    overview = client.get("/api/order", headers=MULTI).json()["order"]
    listing = client.get("/api/order/list", headers=MULTI).json()
    scope = client.get("/api/order/scope", headers=MULTI).json()

    primary = listing["primary_order_id"]
    assert listing["has_order"] is True
    assert overview["order_id"] == primary
    assert scope["order_id"] == primary
    # 双在期时主订单取到期更晚的基础版，专业版只覆盖部分功能。
    assert primary == "O-M2"
    assert listing["order_count"] == 2
    assert listing["in_service_count"] == 2


def test_列表逐条结论与单条判定一致(client):
    """列表摘要里的结论必须能由 /order/scope?order_id= 逐条复现。"""
    listing = client.get("/api/order/list", headers=MULTI).json()
    for item in listing["orders"]:
        scope = client.get(
            f"/api/order/scope?order_id={item['order_id']}", headers=MULTI
        ).json()
        assert scope["order_id"] == item["order_id"]
        for key in ("tier", "tier_label", "order_status", "in_service", "allowed", "reason_code"):
            assert scope[key] == item[key], f"{item['order_id']} 的 {key} 两处不一致"
        assert scope["period"]["days_remaining"] == item["period"]["days_remaining"]
        assert scope["period"]["expires_at"] == item["period"]["expires_at"]


def test_续费提示覆盖全部订单且不把在期订单报成已到期(client):
    """续费视图是按订单逐条算的：订单集合要与列表相同，结论不能自相矛盾。"""
    for headers in (MULTI, EXPIRED, SPREAD):
        listing = client.get("/api/order/list", headers=headers).json()
        renewal = client.get("/api/order/renewal", headers=headers).json()
        by_id = {item["order_id"]: item for item in listing["orders"]}

        assert {hint["order_id"] for hint in renewal["hints"]} == set(by_id)
        assert renewal["order_count"] == listing["order_count"]
        assert renewal["due_count"] + renewal["upcoming_count"] == renewal["remind_count"]

        for hint in renewal["hints"]:
            item = by_id[hint["order_id"]]
            assert hint["order_status"] == item["order_status"]
            assert hint["tier_label"] == item["tier_label"]
            # 提醒只发生在真正需要处理的两档上，其余档位一律不打扰。
            assert hint["remind"] is (hint["state"] in {"upcoming", "due"})
            if item["in_service"]:
                assert hint["state"] in {"active", "upcoming"}
                assert hint["days_remaining"] == item["period"]["days_remaining"]


def test_功能判定按全部订单汇总并给出覆盖来源(client):
    """主订单是基础版，但另一条在期专业版含批量导出：单条拒绝、跨订单放行。"""
    single = client.get("/api/order/scope?feature=批量导出", headers=MULTI).json()
    union = client.get("/api/order/feature-scope?feature=批量导出", headers=MULTI).json()
    listing = client.get("/api/order/list", headers=MULTI).json()

    assert single["order_id"] == "O-M2"
    assert single["allowed"] is False
    assert single["reason_code"] == "feature_not_included"

    assert union["in_scope"] is True
    assert union["covered_by"] == "O-M1"
    assert union["feature"]["included"] is True
    # 覆盖来源必须是一条在期订单，且该版本能力里确实有这项功能。
    active_ids = {item["order_id"] for item in listing["orders"] if item["in_service"]}
    assert union["covered_by"] in active_ids
    assert "批量导出" in listing["features"]
    assert union["order_count"] == listing["order_count"]
    assert union["in_service_count"] == listing["in_service_count"]


def test_管理详情与用户侧结论一致(client):
    """客服看到的单条订单，结论要与客户自己看到的一样。"""
    detail = client.get("/api/order/admin/O-M1", headers=ADMIN).json()
    scope = client.get("/api/order/scope?order_id=O-M1", headers=MULTI).json()
    listing = client.get("/api/order/admin?user_id=u-multi", headers=ADMIN).json()

    for key in ("order_id", "tier", "tier_label", "in_service", "allowed", "reason_code"):
        assert detail[key] == scope[key], f"管理详情与用户判定的 {key} 不一致"
    assert detail["period"] == scope["period"]
    # 归属只在管理侧透出，用户侧判定不返回 user_id。
    assert detail["user_id"] == "u-multi"
    assert detail["user_order_count"] == 2
    assert detail["status_flow"]["order_id"] == "O-M1"
    assert detail["status_flow"]["known"] is True

    summary = next(item for item in listing["items"] if item["order_id"] == "O-M1")
    for key in ("expires_text", "days_remaining", "status", "status_label", "in_service"):
        assert detail[key] == summary[key], f"列表摘要与详情的 {key} 不一致"


# ---------------------------------------------------------------------------
# 四、状态变化后各接口同步收敛
# ---------------------------------------------------------------------------


def test_归档到期订单后各接口同步收紧(client, store):
    """归档只动「状态有效且服务期已结束」的订单，动完各接口要一起变。"""
    before = client.get("/api/order/list", headers=EXPIRED).json()
    assert before["has_order"] is True
    assert all(item["in_service"] is False for item in before["orders"])

    archived = client.post("/api/order/expire", headers=ADMIN).json()
    assert archived["archived"] == ["O-E2"]
    assert archived["has_more"] is False
    assert _status_of(store, "O-E2") == OrderStatus.EXPIRED.value

    after = client.get("/api/order/list", headers=EXPIRED).json()
    assert all(item["in_service"] is False for item in after["orders"])
    assert after["in_service_count"] == 0
    scope = client.get("/api/order/scope", headers=EXPIRED).json()
    assert scope["in_service"] is False
    assert scope["reason_code"] in {"order_inactive", "expired"}
    renewal = client.get("/api/order/renewal", headers=EXPIRED).json()
    assert renewal["due_count"] >= 1
    assert {hint["order_id"] for hint in renewal["hints"]} == {"O-E1", "O-E2"}

    # 重复归档：没有候选就不是错误，结论也不再变化。
    again = client.post("/api/order/expire", headers=ADMIN).json()
    assert again["archived"] == []
    assert again["has_more"] is False
    assert client.get("/api/order/list", headers=EXPIRED).json() == after


def test_退订是终态不可复活(client, store):
    """退订订单不再参与服务判定，也不能被状态机改回有效。"""
    detail = client.get("/api/order/admin/O-S2", headers=ADMIN).json()
    assert detail["status"] == OrderStatus.CANCELLED.value
    assert detail["status_flow"]["terminal"] is True
    assert detail["status_flow"]["allowed_statuses"] == []
    assert detail["in_service"] is False
    assert detail["renewal"]["remind"] is False

    with pytest.raises(OrderStatusTransitionError) as exc_info:
        orders_service.change_order_status("O-S2", OrderStatus.ACTIVE)
    assert exc_info.value.code == "ORDER_INVALID_TRANSITION"
    assert _status_of(store, "O-S2") == OrderStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# 五、数据隔离与鉴权
# ---------------------------------------------------------------------------


def test_客户之间看不到彼此的订单(client):
    """无订单账号的任何查询接口都不该出现别人的订单号。"""
    paths = [
        "/api/order",
        "/api/order/list",
        "/api/order/scope",
        "/api/order/scope?order_id=O-M1",
        "/api/order/feature-scope?feature=批量导出",
        "/api/order/renewal",
    ]
    dumped = ""
    for path in paths:
        response = client.get(path, headers=NONE)
        assert response.status_code == 200, f"{path} 对已登录的无订单账号应正常返回"
        dumped += json.dumps(response.json(), ensure_ascii=False)

    for order_id in ("O-M1", "O-M2", "O-E1", "O-E2", "O-S1", "O-S2", "O-S3"):
        assert order_id not in dumped

    assert client.get("/api/order/list", headers=NONE).json()["order_count"] == 0
    assert client.get("/api/order/scope?order_id=O-M1", headers=NONE).json()["has_order"] is False
    assert client.get("/api/order/feature-scope?feature=批量导出", headers=NONE).json()["has_order"] is False


def test_管理接口只对管理员开放(client):
    """跨客户的订单列表与详情属于管理能力，普通用户与匿名用户都进不来。"""
    for path in ("/api/order/admin", "/api/order/admin/O-M1", "/api/order/expire"):
        method = client.post if path.endswith("/expire") else client.get
        assert method(path).status_code == 401
        forbidden = method(path, headers=MULTI)
        assert forbidden.status_code == 403
        assert forbidden.json()["error_code"] == "TICKET_FORBIDDEN"

    listing = client.get("/api/order/admin?page_size=100", headers=ADMIN).json()
    assert listing["total"] == len(ORDER_SEEDS)
    # 管理员自己没有订单，用户侧视图应当为空而不是看到别人的订单。
    assert client.get("/api/order/list", headers=ADMIN).json()["has_order"] is False


# ---------------------------------------------------------------------------
# 六、排序、分页与畸形入参
# ---------------------------------------------------------------------------


def test_管理列表排序方向互为逆序且同值按订单号升序(client):
    """两个方向取同一批订单，同到期时间的记录在两个方向里都按订单号升序。"""
    asc = client.get("/api/order/admin?order=asc&page_size=100", headers=ADMIN).json()
    desc = client.get("/api/order/admin?order=desc&page_size=100", headers=ADMIN).json()

    assert {item["order_id"] for item in asc["items"]} == {item["order_id"] for item in desc["items"]}
    assert [item["order_id"] for item in asc["items"]] != [item["order_id"] for item in desc["items"]]

    asc_ids = [item["order_id"] for item in asc["items"]]
    desc_ids = [item["order_id"] for item in desc["items"]]
    # 同到期时间的 O-S1 / O-S3 在两个方向里都是 S1 在前。
    assert asc_ids.index("O-S1") < asc_ids.index("O-S3")
    assert desc_ids.index("O-S1") < desc_ids.index("O-S3")

    with_session = client.get("/api/order/admin?page_size=100", headers=ADMIN).json()
    assert [item["order_id"] for item in with_session["items"]] == desc_ids


def test_管理列表翻页拼接不重不漏(client):
    """逐页拼接的结果必须与一次性取回完全一致，翻页不能改变排序。"""
    full = client.get("/api/order/admin?page_size=100", headers=ADMIN).json()
    collected = []
    page = 1
    while True:
        body = client.get(
            f"/api/order/admin?page={page}&page_size=2", headers=ADMIN
        ).json()
        assert body["page"] == page
        assert body["page_size"] == 2
        assert body["total"] == full["total"]
        collected.extend(item["order_id"] for item in body["items"])
        if not body["has_more"]:
            assert body["pages"] == page
            break
        page += 1

    assert collected == [item["order_id"] for item in full["items"]]
    assert len(collected) == len(set(collected))

    # 越界页给空列表，但总数与分组计数不跟着变。
    beyond = client.get("/api/order/admin?page=99&page_size=2", headers=ADMIN).json()
    assert beyond["items"] == []
    assert beyond["total"] == full["total"]
    assert beyond["by_status"] == full["by_status"]


def test_管理列表分组计数覆盖全量而非当前页(client):
    """by_status / by_tier 是筛选后的全量计数，翻页时标签不能跳动。"""
    first = client.get("/api/order/admin?page_size=1&page=1", headers=ADMIN).json()
    last = client.get("/api/order/admin?page_size=1&page=99", headers=ADMIN).json()
    assert first["by_status"] == last["by_status"]
    assert first["by_tier"] == last["by_tier"]
    assert sum(bucket["count"] for bucket in first["by_status"]) == first["total"]
    assert sum(bucket["count"] for bucket in first["by_tier"]) == first["total"]

    active = client.get("/api/order/admin?status=active&page_size=100", headers=ADMIN).json()
    active_seeds = [seed for seed in ORDER_SEEDS if seed[3] == "active"]
    assert active["total"] == len(active_seeds)
    assert {item["order_id"] for item in active["items"]} == {seed[0] for seed in active_seeds}
    # 已知状态即使计数为零也占位，顺序固定；筛选后只有命中的那档非零。
    assert [bucket["key"] for bucket in active["by_status"]] == ["active", "expired", "cancelled"]
    assert [bucket["count"] for bucket in active["by_status"]] == [len(active_seeds), 0, 0]


BAD_CALLS = [
    ("/api/order/scope", {"feature": "功" * 51}, 422),
    ("/api/order/scope", {"order_id": "O" * 65}, 422),
    ("/api/order/scope", {"feature": "量子计算"}, 200),
    ("/api/order/feature-scope", {"feature": ""}, 200),
    ("/api/order/feature-scope", {"feature": "😀😀"}, 200),
    ("/api/order/renewal", {"window_days": -1}, 422),
    ("/api/order/renewal", {"window_days": "abc"}, 422),
    ("/api/order/admin", {"status": "paid"}, 400),
    ("/api/order/admin", {"tier": "ultimate"}, 400),
    ("/api/order/admin", {"page": 0}, 422),
    ("/api/order/admin", {"page_size": 101}, 422),
    ("/api/order/admin", {"order": "sideways"}, 400),
    ("/api/order/admin", {"keyword": "k" * 65}, 400),
    ("/api/order/admin/O-UNKNOWN", {}, 404),
]


@pytest.mark.parametrize("path,params,expected", BAD_CALLS)
def test_畸形入参不产生服务端错误(client, path, params, expected):
    """异常入参要么被业务错误拒绝，要么按空结论返回，绝不能 5xx。"""
    headers = ADMIN if "/admin" in path else MULTI
    response = client.get(path, params=params, headers=headers)
    assert response.status_code == expected, response.text
    assert response.status_code < 500

    body = response.json()
    if response.status_code in (400, 404):
        assert body["error_code"].startswith("ORDER_")
        assert set(body) <= {"error", "error_code", "error_detail"}
        assert body["error"]
    elif response.status_code == 422:
        assert "detail" in body


# ---------------------------------------------------------------------------
# 七、响应契约与空结论
# ---------------------------------------------------------------------------

CONTRACT_KEYS = {
    "/api/order": {"has_order", "order_id", "product_tier", "product_name",
                   "in_service", "features", "expires_at"},    "/api/order/list": {"has_order", "order_count", "in_service_count",
                        "primary_order_id", "orders", "features", "message"},
    "/api/order/scope": {"has_order", "order_id", "tier", "tier_label", "order_status",
                         "period", "features", "feature", "in_service", "allowed",
                         "reason_code", "reason", "message"},
    "/api/order/feature-scope": {"has_order", "feature", "in_scope", "covered_by",
                                 "order_count", "in_service_count", "reason_code",
                                 "reason", "message", "orders"},
    "/api/order/renewal": {"has_order", "order_count", "remind_count", "due_count",
                           "upcoming_count", "window_days", "hints", "message"},
    "/api/order/admin": {"items", "total", "page", "page_size", "pages", "has_more",
                         "order", "statuses", "tiers", "user_id", "keyword",
                         "by_status", "by_tier", "message"},
    "/api/order/admin/O-M1": {"order_id", "user_id", "product_tier", "tier", "tier_label",
                              "product_name", "status", "status_label", "period",
                              "in_service", "expires_text", "days_remaining", "allowed",
                              "reason_code", "reason", "features", "user_order_count",
                              "status_flow", "renewal", "message"},
}


def test_订单接口字段契约总表(client):
    """八个订单接口的顶层字段一次性冻结：多一个少一个都算契约变更。"""
    for path, expected in CONTRACT_KEYS.items():
        headers = ADMIN if "/admin" in path else MULTI
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if path == "/api/order":
            # 总览接口外面包一层 order，是前端约定的取数位置。
            assert set(body) == {"order"}
            body = body["order"]
        assert set(body) == expected, f"{path} 字段集合发生变化"

    # 嵌套块同样是对外契约，缺键会让前端在渲染时直接报错。
    scope = client.get("/api/order/scope", headers=MULTI).json()
    assert set(scope["period"]) == {
        "status", "status_label", "started_at", "expires_at", "started_text",
        "expires_text", "days_total", "days_used", "days_remaining",
        "percent_used", "active", "expired", "not_started",
    }
    detail = client.get("/api/order/admin/O-M1", headers=ADMIN).json()
    assert set(detail["status_flow"]) == {
        "order_id", "status", "status_label", "known", "terminal",
        "allowed_statuses", "allowed_labels",
    }
    assert set(detail["renewal"]) == {
        "order_id", "product_tier", "tier_label", "order_status", "state",
        "state_label", "remind", "urgency", "suggested_action", "window_days",
        "days_remaining", "days_overdue", "expires_text", "message",
    }
    hint = client.get("/api/order/renewal", headers=MULTI).json()["hints"][0]
    assert set(hint) == set(detail["renewal"])
    item = client.get("/api/order/list", headers=MULTI).json()["orders"][0]
    assert set(item) == {
        "order_id", "product_tier", "tier", "tier_label", "order_status",
        "period", "in_service", "allowed", "reason_code", "reason",
    }
    admin_item = client.get("/api/order/admin", headers=ADMIN).json()["items"][0]
    assert set(admin_item) == {
        "order_id", "user_id", "product_tier", "tier", "tier_label", "status",
        "status_label", "period", "in_service", "expires_text", "days_remaining",
    }


def test_无订单账号各接口给空结论(client):
    """没买过东西不是错误：各接口给结构完整的空结论，字段一个不少。"""
    overview = client.get("/api/order", headers=NONE).json()["order"]
    assert overview == {"has_order": False, "message": "未查询到该用户的订单记录"}

    listing = client.get("/api/order/list", headers=NONE).json()
    assert listing["has_order"] is False
    assert listing["order_count"] == 0
    assert listing["in_service_count"] == 0
    assert listing["primary_order_id"] is None
    assert listing["orders"] == []
    assert listing["features"] == []

    scope = client.get("/api/order/scope", headers=NONE).json()
    assert scope["has_order"] is False
    assert scope["order_id"] is None
    assert scope["period"] is None
    assert scope["feature"] is None
    assert scope["in_service"] is False
    assert scope["allowed"] is False
    assert scope["reason_code"] == "no_order"

    feature = client.get("/api/order/feature-scope?feature=批量导出", headers=NONE).json()
    assert feature["has_order"] is False
    assert feature["in_scope"] is False
    assert feature["covered_by"] is None
    assert feature["orders"] == []
    assert feature["reason_code"] == "no_order"

    renewal = client.get("/api/order/renewal", headers=NONE).json()
    assert renewal["has_order"] is False
    assert renewal["order_count"] == 0
    assert renewal["remind_count"] == 0
    assert renewal["hints"] == []
    assert renewal["window_days"] == 30
