"""管理侧订单列表（筛选 / 分页 / 分组计数）测试。

覆盖三块：
1. 入参归一化：状态、版本、客户、关键词、分页、排序的合法与非法写法；
2. 查询结果：筛选叠加、分页口径、排序稳定性、分组计数、服务期结论；
3. 接口层：管理员可查、普通用户与匿名被拒、错误入参走统一错误体。

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

ADMIN = {"Authorization": "Bearer tk-admin-list"}
CUSTOMER = {"Authorization": "Bearer tk-customer-list"}

NOW = time.time()
DAY = 86400.0


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    """临时库 + 三个账号与七条订单，覆盖有效 / 过期 / 退订 / 未知状态四种情形。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'admin_orders.db'}",
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
                Session(token="tk-admin-list", user_id="u-admin", username="admin",
                        name="客服小张", role="admin", expires_at=NOW + 3600),
                Session(token="tk-customer-list", user_id="u-a", username="cust-a",
                        name="客户甲", role="customer", expires_at=NOW + 3600),
                Product(id="basic", name="基础版", features=["基础导出"]),
                Product(id="pro", name="专业版", features=["基础导出", "批量导出"]),
                # u-a：一条在期专业版、一条已归档过期的专业版、一条状态未知的脏数据
                Order(id="O-1001", user_id="u-a", product_tier="pro", status="active",
                      started_at=NOW - 10 * DAY, expires_at=NOW + 30 * DAY),
                Order(id="O-1002", user_id="u-a", product_tier="pro", status="expired",
                      started_at=NOW - 60 * DAY, expires_at=NOW - 5 * DAY),
                Order(id="O-1006", user_id="u-a", product_tier="pro", status="paused",
                      started_at=NOW - 3 * DAY, expires_at=NOW + 3 * DAY),
                # u-b：一条状态有效但服务期已过、一条退订、一条长期在期基础版
                Order(id="O-1003", user_id="u-b", product_tier="basic", status="active",
                      started_at=NOW - 200 * DAY, expires_at=NOW - 100 * DAY),
                Order(id="O-1004", user_id="u-b", product_tier="pro", status="cancelled",
                      started_at=NOW - 20 * DAY, expires_at=NOW + 60 * DAY),
                Order(id="O-1005", user_id="u-b", product_tier="basic", status="active",
                      started_at=NOW - 5 * DAY, expires_at=NOW + 200 * DAY),
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


def _ids(result: dict) -> list[str]:
    return [item["order_id"] for item in result["items"]]


def _counts(items: list[dict]) -> dict:
    return {item["key"]: item["count"] for item in items}


# ---------------------------------------------------------------------------
# 入参归一化：合法与非法写法
# ---------------------------------------------------------------------------


def test_拆分单值与多值筛选写法等价():
    for values in (["active"], "active", ["active,cancelled"], ["active", "cancelled"]):
        selected = orders_service.normalize_admin_statuses(values)
        expected = ["active"] if values in (["active"], "active") else ["active", "cancelled"]
        assert selected == expected


def test_空值与all视为不筛选():
    assert orders_service.normalize_admin_statuses(None) is None
    assert orders_service.normalize_admin_statuses([]) is None
    assert orders_service.normalize_admin_statuses(["all"]) is None
    assert orders_service.normalize_admin_statuses(["", "  "]) is None


def test_状态大小写与空白被归一():
    assert orders_service.normalize_admin_statuses([" ACTIVE "]) == ["active"]


def test_未知状态直接拒绝():
    with pytest.raises(orders_service.OrderQueryError) as exc:
        orders_service.normalize_admin_statuses(["active,unknown"])
    assert exc.value.code == "ORDER_INVALID_FILTER"
    assert exc.value.http_status == 400
    assert exc.value.details["field"] == "status"


def test_版本别名归一为basic与pro():
    assert orders_service.normalize_admin_tiers(["专业版"]) == ["pro"]
    assert orders_service.normalize_admin_tiers(["premium,basic"]) == ["pro", "basic"]


def test_未知版本直接拒绝():
    with pytest.raises(orders_service.OrderQueryError) as exc:
        orders_service.normalize_admin_tiers(["enterprise"])
    assert exc.value.details["field"] == "tier"


def test_客户编号去空白与超长拒绝():
    assert orders_service.normalize_admin_user_id("  u-a ") == "u-a"
    assert orders_service.normalize_admin_user_id("   ") is None
    with pytest.raises(orders_service.OrderQueryError):
        orders_service.normalize_admin_user_id("u" * 65)


def test_关键词归一与超长拒绝():
    assert orders_service.normalize_admin_keyword("  O-1001  ") == "O-1001"
    assert orders_service.normalize_admin_keyword("") is None
    with pytest.raises(orders_service.OrderQueryError):
        orders_service.normalize_admin_keyword("k" * 65)


def test_分页默认值与边界():
    assert orders_service.normalize_admin_pagination(None, None) == (1, 20)
    assert orders_service.normalize_admin_pagination(3, 5) == (3, 5)
    with pytest.raises(orders_service.OrderQueryError):
        orders_service.normalize_admin_pagination(0, 20)
    with pytest.raises(orders_service.OrderQueryError):
        orders_service.normalize_admin_pagination(1, 0)
    with pytest.raises(orders_service.OrderQueryError):
        orders_service.normalize_admin_pagination(1, 101)


def test_排序方向归一与非法拒绝():
    assert orders_service.normalize_admin_order(None) == "desc"
    assert orders_service.normalize_admin_order("ASC") == "asc"
    with pytest.raises(orders_service.OrderQueryError):
        orders_service.normalize_admin_order("created")


# ---------------------------------------------------------------------------
# 查询结果：筛选、分页、排序、计数
# ---------------------------------------------------------------------------


def test_无筛选返回全量并按到期时间倒序(db_session):
    result = orders_service.list_all_orders(now=NOW)
    assert result["total"] == 6
    assert result["order"] == "desc"
    assert _ids(result) == ["O-1005", "O-1004", "O-1001", "O-1006", "O-1002", "O-1003"]
    assert result["statuses"] == [] and result["tiers"] == []
    assert result["user_id"] == "" and result["keyword"] == ""


def test_同到期时间按订单号升序兜底(db_session):
    db = db_session()
    try:
        db.add_all(
            [
                Order(id="O-2002", user_id="u-a", product_tier="pro", status="active",
                      started_at=NOW - DAY, expires_at=NOW + 10 * DAY),
                Order(id="O-2001", user_id="u-a", product_tier="pro", status="active",
                      started_at=NOW - DAY, expires_at=NOW + 10 * DAY),
            ]
        )
        db.commit()
    finally:
        db.close()

    ids = _ids(orders_service.list_all_orders(now=NOW, page_size=100))
    assert ids.index("O-2001") < ids.index("O-2002")


def test_分页切分与末页标记(db_session):
    first = orders_service.list_all_orders(page=1, page_size=4, now=NOW)
    assert first["pages"] == 2 and first["has_more"] is True
    assert len(first["items"]) == 4 and first["total"] == 6

    second = orders_service.list_all_orders(page=2, page_size=4, now=NOW)
    assert second["has_more"] is False
    assert len(second["items"]) == 2
    # 两页之间不重不漏
    assert set(_ids(first)) & set(_ids(second)) == set()
    assert set(_ids(first)) | set(_ids(second)) == {"O-1001", "O-1002", "O-1003",
                                                   "O-1004", "O-1005", "O-1006"}


def test_超出末页返回空列表但保留总数(db_session):
    result = orders_service.list_all_orders(page=9, page_size=4, now=NOW)
    assert result["items"] == []
    assert result["total"] == 6 and result["pages"] == 2 and result["has_more"] is False


def test_按状态筛选(db_session):
    active = orders_service.list_all_orders(statuses=["active"], now=NOW)
    # 状态有效的包含「服务期已过」的那条，筛选口径只看订单状态
    assert set(_ids(active)) == {"O-1001", "O-1003", "O-1005"}
    assert active["statuses"] == ["active"]

    multi = orders_service.list_all_orders(statuses=["expired,cancelled"], now=NOW)
    assert set(_ids(multi)) == {"O-1002", "O-1004"}
    assert multi["statuses"] == ["expired", "cancelled"]


def test_按版本筛选支持别名(db_session):
    pro = orders_service.list_all_orders(tiers=["专业版"], now=NOW)
    assert set(_ids(pro)) == {"O-1001", "O-1002", "O-1004", "O-1006"}
    assert pro["tiers"] == ["pro"]

    both = orders_service.list_all_orders(tiers=["pro,basic"], now=NOW)
    assert both["total"] == 6


def test_按客户筛选(db_session):
    mine = orders_service.list_all_orders(user_id="u-b", now=NOW)
    assert set(_ids(mine)) == {"O-1003", "O-1004", "O-1005"}
    assert mine["user_id"] == "u-b"

    none = orders_service.list_all_orders(user_id="u-missing", now=NOW)
    assert none["total"] == 0 and none["items"] == []


def test_关键词匹配订单号与客户编号(db_session):
    by_order = orders_service.list_all_orders(keyword="o-1001", now=NOW)
    assert _ids(by_order) == ["O-1001"]
    assert by_order["keyword"] == "o-1001"

    by_user = orders_service.list_all_orders(keyword="u-b", now=NOW)
    assert set(_ids(by_user)) == {"O-1003", "O-1004", "O-1005"}


def test_关键词中的通配符按字面量匹配(db_session):
    assert orders_service.list_all_orders(keyword="%", now=NOW)["total"] == 0
    assert orders_service.list_all_orders(keyword="_", now=NOW)["total"] == 0


def test_空白关键词回退为不筛选(db_session):
    result = orders_service.list_all_orders(keyword="   ", now=NOW)
    assert result["total"] == 6 and result["keyword"] == ""


def test_筛选条件可叠加(db_session):
    result = orders_service.list_all_orders(
        statuses=["active"], tiers=["basic"], user_id="u-b", now=NOW
    )
    assert set(_ids(result)) == {"O-1003", "O-1005"}

    miss = orders_service.list_all_orders(
        statuses=["active"], tiers=["pro"], user_id="u-b", now=NOW
    )
    assert miss["total"] == 0


def test_排序方向切换(db_session):
    asc = orders_service.list_all_orders(order="asc", now=NOW)
    assert _ids(asc)[0] == "O-1003" and asc["order"] == "asc"
    assert _ids(asc) == list(reversed(_ids(orders_service.list_all_orders(now=NOW))))


def test_分组计数覆盖全部命中结果而不受分页影响(db_session):
    result = orders_service.list_all_orders(page=1, page_size=2, now=NOW)
    assert len(result["items"]) == 2
    assert _counts(result["by_status"]) == {
        "active": 3, "expired": 1, "cancelled": 1, "paused": 1,
    }
    assert _counts(result["by_tier"]) == {"basic": 2, "pro": 4}


def test_分组计数随筛选条件收窄(db_session):
    result = orders_service.list_all_orders(statuses=["active"], now=NOW)
    assert _counts(result["by_status"]) == {
        "active": 3, "expired": 0, "cancelled": 0,
    }
    assert _counts(result["by_tier"]) == {"basic": 2, "pro": 1}


def test_空结果的分组计数全为零(db_session):
    result = orders_service.list_all_orders(statuses=["cancelled"], tiers=["basic"], now=NOW)
    assert result["total"] == 0
    assert _counts(result["by_status"]) == {"active": 0, "expired": 0, "cancelled": 0}
    assert _counts(result["by_tier"]) == {"basic": 0, "pro": 0}


def test_列表项带服务期结论(db_session):
    result = orders_service.list_all_orders(now=NOW, page_size=100)
    items = {item["order_id"]: item for item in result["items"]}

    assert items["O-1001"]["in_service"] is True
    assert items["O-1001"]["tier"] == "pro"
    assert items["O-1001"]["tier_label"] == "专业版"
    assert items["O-1001"]["status_label"] == "有效"
    assert items["O-1001"]["user_id"] == "u-a"
    assert items["O-1001"]["days_remaining"] > 0
    assert items["O-1001"]["expires_text"]

    # 状态有效但服务期已过：不能因为订单状态是 active 就认为还能用
    assert items["O-1003"]["status"] == "active"
    assert items["O-1003"]["in_service"] is False

    # 退订订单服务立即停止
    assert items["O-1004"]["status_label"] == "退订"
    assert items["O-1004"]["in_service"] is False

    # 未知状态照原值回显，展示名用兜底文案
    assert items["O-1006"]["status"] == "paused"
    assert items["O-1006"]["status_label"] == "未知状态"


def test_摘要文案包含筛选条件与页码(db_session):
    plain = orders_service.list_all_orders(now=NOW)
    assert plain["message"].startswith("共 6 个订单")
    assert "有效 3 个" in plain["message"]

    filtered = orders_service.list_all_orders(statuses=["active"], tiers=["pro"],
                                             page=1, page_size=1, now=NOW)
    assert "按 状态 有效 + 版本 专业版 筛选到" in filtered["message"]
    assert "当前第 1/1 页" not in filtered["message"] or filtered["pages"] > 1


# ---------------------------------------------------------------------------
# 接口层：权限与错误契约
# ---------------------------------------------------------------------------


def test_管理员可查询列表(client):
    response = client.get("/api/order/admin", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 6
    assert body["page"] == 1 and body["page_size"] == 20 and body["pages"] == 1
    assert len(body["items"]) == 6
    assert set(body["items"][0]) >= {
        "order_id", "user_id", "product_tier", "tier", "tier_label",
        "status", "status_label", "period", "in_service", "expires_text",
    }
    assert body["by_status"] and body["by_tier"]


def test_接口支持重复传参与多值筛选(client):
    response = client.get(
        "/api/order/admin?status=active&status=expired&tier=pro&page_size=100",
        headers=ADMIN,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["statuses"] == ["active", "expired"]
    assert body["tiers"] == ["pro"]
    assert set(item["order_id"] for item in body["items"]) == {"O-1001", "O-1002"}


def test_普通用户不可访问管理列表(client):
    response = client.get("/api/order/admin", headers=CUSTOMER)
    assert response.status_code == 403
    assert response.json()["error_code"] == "TICKET_FORBIDDEN"


def test_匿名不可访问管理列表(client):
    response = client.get("/api/order/admin")
    assert response.status_code == 401
    assert response.json()["error_code"] == "TICKET_UNAUTHORIZED"


def test_未知状态返回统一错误体(client):
    response = client.get("/api/order/admin?status=unknown", headers=ADMIN)
    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "ORDER_INVALID_FILTER"
    assert "未知的订单状态" in body["error"]


def test_非法排序返回统一错误体(client):
    response = client.get("/api/order/admin?order=created", headers=ADMIN)
    assert response.status_code == 400
    assert response.json()["error_code"] == "ORDER_INVALID_FILTER"


def test_分页越界由框架拒绝(client):
    assert client.get("/api/order/admin?page=0", headers=ADMIN).status_code == 422
    assert client.get("/api/order/admin?page_size=101", headers=ADMIN).status_code == 422


def test_关键词与客户筛选走查询参数(client):
    response = client.get("/api/order/admin?keyword=u-b&status=active", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["keyword"] == "u-b"
    assert set(item["order_id"] for item in body["items"]) == {"O-1003", "O-1005"}
