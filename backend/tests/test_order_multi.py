"""多订单支持测试：一个用户持有多个产品时怎么取、怎么挑、怎么汇总。

覆盖四组：

- 取数：全部订单按到期时间倒序返回，与该用户的其它数据无关；
- 挑选：主订单的服务中优先、到期晚优先、订单号兜底，且结论稳定；
- 精确取：指定订单号只取本人那一单，别人的订单号不会被放行；
- 汇总：多订单视图的计数、能力并集与文案，以及逐单判定与主订单判定的差别。

时间一律由用例传入 now，不依赖真实时钟。
"""

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Order, Product
from backend.services import orders

DAY = 86400


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 两个产品的配置行，服务层 SessionLocal 指向它。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'multi.db'}",
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
                Product(
                    id="pro",
                    name="专业版",
                    features=["批量导出", "高级报表", "优先客服支持", "API 接口"],
                ),
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
        "id": "O-8001",
        "user_id": "u-multi",
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


def _ids(items) -> list[str]:
    return [item["order_id"] for item in items]


# ---------------------------------------------------------------------------
# 一、取数
# ---------------------------------------------------------------------------


def test_list_orders_returns_all_of_the_user_only(factory):
    _add_order(factory, id="O-8101", expires_at=time.time() - 10 * DAY)
    _add_order(factory, id="O-8102", expires_at=time.time() + 10 * DAY)
    _add_order(factory, id="O-8103", user_id="u-other")

    rows = orders.list_orders("u-multi")

    assert [row["id"] for row in rows] == ["O-8102", "O-8101"]


def test_list_orders_breaks_expiry_ties_by_id(factory):
    same = time.time() + 20 * DAY
    _add_order(factory, id="O-8202", expires_at=same)
    _add_order(factory, id="O-8201", expires_at=same)

    assert [row["id"] for row in orders.list_orders("u-multi")] == ["O-8201", "O-8202"]


def test_list_orders_without_any_order(factory):
    assert orders.list_orders("u-nobody") == []


# ---------------------------------------------------------------------------
# 二、主订单挑选
# ---------------------------------------------------------------------------


def test_primary_prefers_order_in_service(factory):
    """基础版在期、专业版已过期时，主订单仍然是基础版。"""
    now = time.time()
    expired_pro = {
        "id": "O-8301",
        "product_tier": "pro",
        "status": "active",
        "started_at": now - 400 * DAY,
        "expires_at": now - 100 * DAY,
    }
    active_basic = {
        "id": "O-8302",
        "product_tier": "basic",
        "status": "active",
        "started_at": now - 10 * DAY,
        "expires_at": now + 20 * DAY,
    }

    primary = orders.select_primary_order([expired_pro, active_basic], now=now)

    assert primary["id"] == "O-8302"


def test_primary_prefers_later_expiry_among_active_orders(factory):
    _add_order(factory, id="O-8401", product_tier="basic", expires_at=time.time() + 10 * DAY)
    _add_order(factory, id="O-8402", product_tier="pro", expires_at=time.time() + 200 * DAY)

    assert orders.get_order("u-multi")["id"] == "O-8402"


def test_primary_falls_back_to_latest_expiry_when_all_expired(factory):
    now = time.time()
    _add_order(factory, id="O-8501", started_at=now - 400 * DAY, expires_at=now - 200 * DAY)
    _add_order(factory, id="O-8502", started_at=now - 100 * DAY, expires_at=now - 10 * DAY)

    primary = orders.select_primary_order(orders.list_orders("u-multi"), now=now)

    assert primary["id"] == "O-8502"


def test_primary_skips_inactive_order_when_others_are_active(factory):
    """退订订单即使到期更晚，也不该压过正常订单。"""
    now = time.time()
    cancelled = {
        "id": "O-8601",
        "product_tier": "pro",
        "status": "cancelled",
        "started_at": now - 10 * DAY,
        "expires_at": now + 300 * DAY,
    }
    normal = {
        "id": "O-8602",
        "product_tier": "basic",
        "status": "active",
        "started_at": now - 10 * DAY,
        "expires_at": now + 10 * DAY,
    }

    assert orders.select_primary_order([cancelled, normal], now=now)["id"] == "O-8602"


def test_primary_selection_is_stable(factory):
    """同一批订单反复挑选必须得到同一条，否则对话里前后会自相矛盾。"""
    same = time.time() + 50 * DAY
    _add_order(factory, id="O-8701", product_tier="basic", expires_at=same)
    _add_order(factory, id="O-8702", product_tier="pro", expires_at=same)

    picked = {orders.get_order("u-multi")["id"] for _ in range(5)}

    assert picked == {"O-8701"}


def test_select_primary_order_ignores_empty_input(factory):
    assert orders.select_primary_order([]) is None
    assert orders.select_primary_order([None, {}]) is None


# ---------------------------------------------------------------------------
# 三、精确取单
# ---------------------------------------------------------------------------


def test_get_order_by_id_returns_the_requested_one(factory):
    _add_order(factory, id="O-8801", product_tier="basic", expires_at=time.time() + 10 * DAY)
    _add_order(factory, id="O-8802", product_tier="pro", expires_at=time.time() + 100 * DAY)

    target = orders.get_order("u-multi", "O-8801")

    assert target["id"] == "O-8801"
    assert target["product_tier"] == "basic"


def test_get_order_by_id_rejects_other_users_order(factory):
    _add_order(factory, id="O-8901", user_id="u-other")

    assert orders.get_order("u-multi", "O-8901") is None


def test_get_order_by_unknown_id(factory):
    _add_order(factory, id="O-8902")

    assert orders.get_order("u-multi", "O-0000") is None


def test_get_order_without_order(factory):
    assert orders.get_order("u-nobody") is None


# ---------------------------------------------------------------------------
# 四、多订单汇总
# ---------------------------------------------------------------------------


def test_query_orders_without_order(factory):
    result = orders.query_orders("u-nobody")

    assert result["has_order"] is False
    assert result["order_count"] == 0
    assert result["in_service_count"] == 0
    assert result["primary_order_id"] is None
    assert result["orders"] == []
    assert result["features"] == []
    assert result["message"] == "未查询到该用户的订单记录"


def test_query_orders_single_order(factory):
    _add_order(factory, id="O-9001")

    result = orders.query_orders("u-multi")

    assert result["has_order"] is True
    assert result["order_count"] == 1
    assert result["in_service_count"] == 1
    assert result["primary_order_id"] == "O-9001"
    assert _ids(result["orders"]) == ["O-9001"]
    assert result["message"] == "共 1 个订单，均在服务期内"


def test_query_orders_unions_features_of_active_orders(factory):
    """基础版与专业版同时有效时，能力取并集而不是只看主订单。"""
    now = time.time()
    _add_order(factory, id="O-9101", product_tier="basic",
               started_at=now - 5 * DAY, expires_at=now + 30 * DAY)
    _add_order(factory, id="O-9102", product_tier="pro",
               started_at=now - 5 * DAY, expires_at=now + 300 * DAY)

    result = orders.query_orders("u-multi")

    assert result["order_count"] == 2
    assert result["in_service_count"] == 2
    assert result["primary_order_id"] == "O-9102"
    # 专业版能力在前（订单更晚到期、排得更靠前），基础版独有的能力补在后面
    assert result["features"] == [
        "批量导出", "高级报表", "优先客服支持", "API 接口",
        "基础导出", "单文件上传", "社区支持",
    ]
    assert result["message"] == "共 2 个订单，均在服务期内"


def test_query_orders_counts_partially_active_orders(factory):
    now = time.time()
    _add_order(factory, id="O-9201", product_tier="basic",
               started_at=now - 5 * DAY, expires_at=now + 30 * DAY)
    _add_order(factory, id="O-9202", product_tier="pro",
               started_at=now - 400 * DAY, expires_at=now - 100 * DAY)

    result = orders.query_orders("u-multi")

    assert result["order_count"] == 2
    assert result["in_service_count"] == 1
    assert result["features"] == ["基础导出", "单文件上传", "社区支持"]
    assert result["message"] == "共 2 个订单，1 个在服务期内"


def test_query_orders_all_expired(factory):
    now = time.time()
    _add_order(factory, id="O-9301", started_at=now - 400 * DAY, expires_at=now - 200 * DAY)
    _add_order(factory, id="O-9302", product_tier="basic",
               started_at=now - 300 * DAY, expires_at=now - 100 * DAY)

    result = orders.query_orders("u-multi")

    assert result["in_service_count"] == 0
    assert result["features"] == []
    assert result["message"] == "共 2 个订单，当前均不在服务期内"
    assert all(item["in_service"] is False for item in result["orders"])


def test_query_orders_keeps_per_order_reason(factory):
    now = time.time()
    _add_order(factory, id="O-9401", product_tier="basic",
               started_at=now - 5 * DAY, expires_at=now + 30 * DAY)
    _add_order(factory, id="O-9402", product_tier="pro", status="cancelled",
               started_at=now - 5 * DAY, expires_at=now + 90 * DAY)

    result = orders.query_orders("u-multi")
    by_id = {item["order_id"]: item for item in result["orders"]}

    assert by_id["O-9401"]["reason_code"] == "in_service"
    assert by_id["O-9402"]["reason_code"] == "order_inactive"
    assert by_id["O-9402"]["in_service"] is False
    assert by_id["O-9402"]["tier_label"] == "专业版"


def test_query_orders_falls_back_to_matrix_without_product_rows(factory):
    _add_order(factory, id="O-9501", product_tier="pro")
    db = factory()
    try:
        db.query(Product).delete()
        db.commit()
    finally:
        db.close()

    result = orders.query_orders("u-multi")

    assert len(result["features"]) == len(orders.TIER_FEATURES["pro"])
    assert "批量导出" in result["features"]


# ---------------------------------------------------------------------------
# 五、逐单判定与既有契约
# ---------------------------------------------------------------------------


def test_describe_order_by_id_targets_that_order(factory):
    now = time.time()
    _add_order(factory, id="O-9601", product_tier="basic",
               started_at=now - 5 * DAY, expires_at=now + 30 * DAY)
    _add_order(factory, id="O-9602", product_tier="pro",
               started_at=now - 5 * DAY, expires_at=now + 300 * DAY)

    basic = orders.describe_order("u-multi", feature="批量导出", now=now, order_id="O-9601")
    pro = orders.describe_order("u-multi", feature="批量导出", now=now, order_id="O-9602")

    assert basic["order_id"] == "O-9601"
    assert basic["allowed"] is False
    assert basic["reason_code"] == "feature_not_included"
    assert pro["order_id"] == "O-9602"
    assert pro["allowed"] is True


def test_describe_order_defaults_to_primary(factory):
    now = time.time()
    _add_order(factory, id="O-9701", product_tier="basic",
               started_at=now - 5 * DAY, expires_at=now + 30 * DAY)
    _add_order(factory, id="O-9702", product_tier="pro",
               started_at=now - 5 * DAY, expires_at=now + 300 * DAY)

    detail = orders.describe_order("u-multi", now=now)

    assert detail["order_id"] == "O-9702"
    assert detail["tier"] == "pro"


def test_describe_order_with_other_users_order_id(factory):
    _add_order(factory, id="O-9801", user_id="u-other")

    detail = orders.describe_order("u-multi", order_id="O-9801")

    assert detail["has_order"] is False
    assert detail["order_id"] is None
    assert detail["reason_code"] == "no_order"


def test_query_order_contract_survives_multi_orders(factory):
    """多订单下 query_order 仍是 7 键契约，且取的是主订单。"""
    now = time.time()
    _add_order(factory, id="O-9901", product_tier="basic",
               started_at=now - 5 * DAY, expires_at=now + 30 * DAY)
    _add_order(factory, id="O-9902", product_tier="pro",
               started_at=now - 5 * DAY, expires_at=now + 300 * DAY)

    result = orders.query_order("u-multi")

    assert set(result) == {
        "has_order", "order_id", "product_tier", "product_name",
        "in_service", "features", "expires_at",
    }
    assert result["order_id"] == "O-9902"
    assert result["product_name"] == "专业版"
