"""订单服务范围判定测试。

三块能力各自成组，同时校验它们拼起来之后的结论：

- 版本能力矩阵：基础版与专业版的能力边界、别名收敛、功能名解析；
- 服务期：有效 / 未开始 / 已到期三态、边界时刻、天数与比例口径、脏数据；
- 综合判定：版本 + 服务期 + 功能三层优先级，以及 query_order 的对外契约。

时间一律由用例显式传入 now，不依赖真实时钟，避免跨零点抖动。
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
        f"sqlite:///{tmp_path / 'orders.db'}",
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


def _add_order(factory, **overrides) -> dict:
    """插入一条订单并返回其字典形态，默认是服务中的专业版订单。"""
    now = time.time()
    payload = {
        "id": "O-9001",
        "user_id": "u-scope",
        "product_tier": "pro",
        "status": "active",
        "started_at": now - 90 * DAY,
        "expires_at": now + 90 * DAY,
    }
    payload.update(overrides)
    db = factory()
    try:
        db.add(Order(**payload))
        db.commit()
        return db.query(Order).filter(Order.id == payload["id"]).first().to_dict()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 一、版本能力矩阵
# ---------------------------------------------------------------------------


def test_pro_capabilities_are_superset_of_basic():
    """专业版覆盖基础版全部能力，升级不会丢功能。"""
    basic = orders.tier_capabilities("basic")
    pro = orders.tier_capabilities("pro")

    assert basic["known"] is True and pro["known"] is True
    assert basic["label"] == "基础版" and pro["label"] == "专业版"
    assert set(basic["feature_codes"]) < set(pro["feature_codes"])
    assert len(pro["features"]) == len(set(pro["features"]))


def test_basic_and_pro_differ_on_key_capabilities():
    """能力边界落在具体几项上：批量导出、高级报表、优先客服、API。"""
    for feature in ("批量导出", "高级报表", "优先客服支持", "API 接口"):
        assert orders.has_capability("pro", feature) is True, feature
        assert orders.has_capability("basic", feature) is False, feature

    for feature in ("基础导出", "单文件上传", "社区支持"):
        assert orders.has_capability("basic", feature) is True, feature
        assert orders.has_capability("pro", feature) is True, feature


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pro", "pro"),
        (" PRO ", "pro"),
        ("专业版", "pro"),
        ("专业", "pro"),
        ("professional", "pro"),
        ("basic", "basic"),
        ("基础版", "basic"),
        ("Standard", "basic"),
        ("enterprise", None),
        ("", None),
        ("   ", None),
        (None, None),
        (123, None),
    ],
)
def test_normalize_tier_covers_aliases_and_dirty_values(raw, expected):
    assert orders.normalize_tier(raw) == expected


def test_unknown_tier_has_no_capabilities():
    caps = orders.tier_capabilities("enterprise")

    assert caps["known"] is False
    assert caps["tier"] is None
    assert caps["features"] == []
    assert caps["label"] == "未知版本"
    assert orders.has_capability("enterprise", "批量导出") is False


@pytest.mark.parametrize(
    "text,code",
    [
        ("批量导出", "export.batch"),
        ("export.batch", "export.batch"),
        ("导出", "export.basic"),
        ("批量导出失败", "export.batch"),
        ("导出按钮没反应", "export.basic"),
        ("API", "api.access"),
        ("人工客服", "support.priority"),
        ("报表", "report.advanced"),
        ("区块链", None),
        ("", None),
        (None, None),
    ],
)
def test_resolve_feature_prefers_exact_then_longest(text, code):
    """标准名精确命中；口语按最长片段命中，「导出」不会被「批量导出」抢走。"""
    feature = orders.resolve_feature(text)
    assert (feature.code if feature else None) == code


def test_has_capability_rejects_unresolvable_feature():
    """解析不出的功能按不包含处理，不猜也不放行。"""
    assert orders.has_capability("pro", "区块链审计") is False
    assert orders.has_capability("pro", "") is False
    assert orders.has_capability(None, "批量导出") is False


# ---------------------------------------------------------------------------
# 二、服务期
# ---------------------------------------------------------------------------


def test_period_active_reports_days_and_percent():
    start, end = 1_700_000_000.0, 1_700_000_000.0 + 100 * DAY
    now = start + 25 * DAY

    period = orders.evaluate_service_period(start, end, now)

    assert period["status"] == orders.SERVICE_ACTIVE
    assert period["active"] is True and period["expired"] is False
    assert period["days_total"] == 100
    assert period["days_used"] == 25
    assert period["days_remaining"] == 75
    assert period["percent_used"] == 25.0
    assert period["started_text"] and period["expires_text"]


def test_period_boundaries_are_left_closed():
    """开始当刻即服务中，到期当刻即已到期。"""
    start, end = 1_700_000_000.0, 1_700_000_000.0 + 10 * DAY

    just_started = orders.evaluate_service_period(start, end, start)
    assert just_started["status"] == orders.SERVICE_ACTIVE
    assert just_started["days_used"] == 0
    assert just_started["percent_used"] == 0.0

    just_ended = orders.evaluate_service_period(start, end, end)
    assert just_ended["status"] == orders.SERVICE_EXPIRED
    assert just_ended["days_remaining"] == 0
    assert just_ended["percent_used"] == 100.0


def test_period_not_started_and_expired():
    start, end = 1_700_000_000.0, 1_700_000_000.0 + 30 * DAY

    pending = orders.evaluate_service_period(start, end, start - DAY)
    assert pending["status"] == orders.SERVICE_NOT_STARTED
    assert pending["not_started"] is True
    assert pending["active"] is False
    assert pending["days_remaining"] == 31  # 未开始时把开始前的等待也算进剩余

    expired = orders.evaluate_service_period(start, end, end + 5 * DAY)
    assert expired["status"] == orders.SERVICE_EXPIRED
    assert expired["expired"] is True
    assert expired["days_remaining"] == 0
    assert expired["percent_used"] == 100.0


def test_period_remaining_days_rounds_up():
    """剩余不足一天按一天算，展示上不会出现「剩余 0 天」却仍在服务中。"""
    start, end = 1_700_000_000.0, 1_700_000_000.0 + 10 * DAY

    period = orders.evaluate_service_period(start, end, end - 3600)

    assert period["status"] == orders.SERVICE_ACTIVE
    assert period["days_remaining"] == 1


@pytest.mark.parametrize(
    "start,end",
    [
        (None, 1_700_000_000.0),
        (1_700_000_000.0, None),
        (1_700_000_000.0, 1_700_000_000.0),
        (1_700_000_000.0, 1_600_000_000.0),
        ("2026-01-01", "2026-12-31"),
    ],
)
def test_period_unknown_for_broken_timestamps(start, end):
    """起止缺失、颠倒、非数值都判为无法判定，不硬算成有效或到期。"""
    period = orders.evaluate_service_period(start, end, 1_700_000_000.0)

    assert period["status"] == orders.SERVICE_UNKNOWN
    assert period["active"] is False
    assert period["expired"] is False
    assert period["days_total"] == 0


# ---------------------------------------------------------------------------
# 三、综合判定
# ---------------------------------------------------------------------------


def test_scope_without_order():
    scope = orders.evaluate_service_scope(None)

    assert scope["has_order"] is False
    assert scope["allowed"] is False
    assert scope["reason_code"] == "no_order"

    assert orders.evaluate_service_scope({"has_order": False})["reason_code"] == "no_order"


def test_scope_in_service_with_included_feature(factory):
    order = _add_order(factory)

    scope = orders.evaluate_service_scope(order, feature="批量导出")

    assert scope["in_service"] is True
    assert scope["allowed"] is True
    assert scope["tier"] == "pro" and scope["tier_label"] == "专业版"
    assert scope["feature"]["code"] == "export.batch"
    assert scope["feature_included"] is True
    assert scope["reason_code"] == "in_service"


def test_scope_feature_not_included_in_basic(factory):
    order = _add_order(factory, id="O-9002", product_tier="basic")

    scope = orders.evaluate_service_scope(order, feature="批量导出")

    assert scope["in_service"] is True
    assert scope["feature_included"] is False
    assert scope["allowed"] is False
    assert scope["reason_code"] == "feature_not_included"
    assert "基础版" in scope["reason"] and "批量导出" in scope["reason"]


def test_scope_unknown_feature_is_not_allowed(factory):
    order = _add_order(factory)

    scope = orders.evaluate_service_scope(order, feature="区块链审计")

    assert scope["allowed"] is False
    assert scope["reason_code"] == "feature_unknown"
    assert "区块链审计" in scope["reason"]


def test_scope_reports_expiry_before_feature(factory):
    """服务已到期时先报到期：先解决续费，再谈版本是否含该功能。"""
    order = _add_order(factory, id="O-9003", product_tier="basic",
                       started_at=time.time() - 400 * DAY,
                       expires_at=time.time() - 40 * DAY)

    scope = orders.evaluate_service_scope(order, feature="批量导出")

    assert scope["in_service"] is False
    assert scope["reason_code"] == "expired"
    assert "已到期" not in scope["reason"]  # 文案走 SCOPE_REASONS，避免与状态名混用
    assert "服务期已结束" in scope["reason"]


def test_scope_not_started_order(factory):
    order = _add_order(factory, id="O-9004",
                       started_at=time.time() + 10 * DAY,
                       expires_at=time.time() + 100 * DAY)

    scope = orders.evaluate_service_scope(order)

    assert scope["in_service"] is False
    assert scope["reason_code"] == "not_started"
    assert scope["period"]["status"] == orders.SERVICE_NOT_STARTED


def test_scope_inactive_order_blocks_service(factory):
    """退订、退款的订单即使还在时间窗口内也不提供服务。"""
    order = _add_order(factory, id="O-9005", status="refunded")

    scope = orders.evaluate_service_scope(order, feature="批量导出")

    assert scope["in_service"] is False
    assert scope["allowed"] is False
    assert scope["reason_code"] == "order_inactive"


def test_scope_unknown_tier(factory):
    order = _add_order(factory, id="O-9006", product_tier="enterprise")

    scope = orders.evaluate_service_scope(order, feature="批量导出")

    assert scope["known_tier"] is False
    assert scope["in_service"] is True  # 时间维度仍在服务期
    assert scope["allowed"] is False
    assert scope["reason_code"] == "tier_unknown"


def test_scope_period_unknown_for_missing_timestamps(factory):
    order = _add_order(factory, id="O-9007", started_at=0.0, expires_at=0.0)

    scope = orders.evaluate_service_scope(order)

    assert scope["reason_code"] == "period_unknown"
    assert scope["in_service"] is False


# ---------------------------------------------------------------------------
# 四、查询入口的对外契约
# ---------------------------------------------------------------------------


def test_query_order_keeps_contract_and_uses_scope(factory):
    _add_order(factory, id="O-9101", user_id="u-pro")

    result = orders.query_order("u-pro")

    assert set(result) == {
        "has_order", "order_id", "product_tier", "product_name",
        "in_service", "features", "expires_at",
    }
    assert result["has_order"] is True
    assert result["in_service"] is True
    assert result["product_name"] == "专业版"
    # 能力列表以产品表配置为准，运营改配置即可生效
    assert result["features"] == ["批量导出", "高级报表", "优先客服支持", "API 接口"]


def test_query_order_marks_expired_order_out_of_service(factory):
    now = time.time()
    _add_order(factory, id="O-9102", user_id="u-expired",
               started_at=now - 400 * DAY, expires_at=now - DAY)

    result = orders.query_order("u-expired")

    assert result["has_order"] is True
    assert result["in_service"] is False


def test_query_order_treats_not_started_as_out_of_service(factory):
    """原实现只看 expires_at，未开始的服务期会被误判为在服务中。"""
    now = time.time()
    _add_order(factory, id="O-9103", user_id="u-pending",
               started_at=now + 5 * DAY, expires_at=now + 100 * DAY)

    assert orders.query_order("u-pending")["in_service"] is False


def test_query_order_without_order(factory):
    result = orders.query_order("u-nobody")

    assert result == {"has_order": False, "message": "未查询到该用户的订单记录"}


def test_query_order_falls_back_to_matrix_without_product_row(factory):
    """产品表缺行时用内置能力矩阵兜底，不把用户判成没有任何功能。"""
    _add_order(factory, id="O-9104", user_id="u-noproduct")
    db = factory()
    try:
        db.query(Product).delete()
        db.commit()
    finally:
        db.close()

    result = orders.query_order("u-noproduct")

    assert result["product_name"] == "专业版"
    assert "批量导出" in result["features"]
    assert len(result["features"]) == len(orders.TIER_FEATURES["pro"])
