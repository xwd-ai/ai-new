"""订单状态流转校验测试。

覆盖五组：

- 流转表：3×3 全矩阵与独立写的契约快照双向核对，不靠读实现自证；
- 校验函数：未知状态、空值与非法流转的错误码与上下文可区分；
- 幂等：重复置为同一状态按通过处理，禁用同状态时给流转错误；
- 写库：合法流转真正落库、非法流转与越权访问不碰数据库；
- 联动：状态变更后服务范围判定随之收紧，退订即为终态。

时间一律由用例传入 now，不依赖真实时钟。
"""

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Order, Product
from backend.models.order import (
    ORDER_STATUS_LABELS,
    OrderErrorCode,
    OrderNotFoundError,
    OrderStatus,
    OrderStatusTransitionError,
    can_transition,
    is_terminal,
    next_statuses,
    validate_transition,
)
from backend.services import orders

DAY = 86400

# 契约快照：故意在用例里独立写一遍，与实现里的流转表互相印证。
# 退订是终态，过期可以续费回有效，有效可以归档为过期或退订。
EXPECTED_TRANSITIONS = {
    "active": {"expired", "cancelled"},
    "expired": {"active", "cancelled"},
    "cancelled": set(),
}


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 产品配置行，订单服务的 SessionLocal 指向它。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'status.db'}",
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
    now = time.time()
    payload = {
        "id": "O-9001",
        "user_id": "u-status",
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


def _stored_status(factory, order_id: str = "O-9001") -> str:
    db = factory()
    try:
        return db.query(Order).filter(Order.id == order_id).one().status
    finally:
        db.close()


def _stored_order(factory, order_id: str = "O-9001") -> dict:
    db = factory()
    try:
        return db.query(Order).filter(Order.id == order_id).one().to_dict()
    finally:
        db.close()


def test_流转表与契约快照一致():
    """合法流转表必须与用例里独立写下的契约完全一致。"""
    for current in OrderStatus:
        actual = {
            target.value for target in OrderStatus if can_transition(current, target)
        }
        assert actual == EXPECTED_TRANSITIONS[current.value], current.value


def test_同状态不在流转表内():
    """同状态不算流转：幂等由校验函数的开关控制，不放进流转表。"""
    for status in OrderStatus:
        assert can_transition(status, status) is False


def test_校验函数覆盖全矩阵():
    """合法组合返回目标状态枚举，非法组合抛流转错误且带原/目标状态。"""
    for current in OrderStatus:
        for target in OrderStatus:
            if target.value in EXPECTED_TRANSITIONS[current.value]:
                assert validate_transition(current, target) is target
                assert validate_transition(current.value, target.value) is target
            elif target != current:
                with pytest.raises(OrderStatusTransitionError) as exc:
                    validate_transition(current.value, target.value)
                assert exc.value.code == OrderErrorCode.INVALID_TRANSITION
                assert exc.value.details == {
                    "from_status": current.value,
                    "to_status": target.value,
                }


def test_未知状态与空值给出独立错误码():
    """状态不可识别与业务不允许的流转分开给码，调用方才能区别处理。"""
    cases = [
        ("", "expired"),
        ("paused", "expired"),
        (None, "expired"),
        ("active", ""),
        ("active", "refunded"),
        ("active", None),
    ]
    for current, target in cases:
        with pytest.raises(OrderStatusTransitionError) as exc:
            validate_transition(current, target)
        assert exc.value.code == OrderErrorCode.UNKNOWN_STATUS
        assert exc.value.details["from_status"] == ("" if current is None else current)
        assert exc.value.details["to_status"] == ("" if target is None else target)
        assert exc.value.to_payload()["error_code"] == OrderErrorCode.UNKNOWN_STATUS


def test_同状态按开关处理():
    """默认把重复置为同一状态视为通过；显式关闭时给出流转错误。"""
    assert validate_transition("active", "active") is OrderStatus.ACTIVE
    assert validate_transition(OrderStatus.EXPIRED, OrderStatus.EXPIRED) is OrderStatus.EXPIRED

    with pytest.raises(OrderStatusTransitionError) as exc:
        validate_transition("cancelled", "cancelled", allow_same=False)
    assert exc.value.code == OrderErrorCode.INVALID_TRANSITION
    assert "退订" in exc.value.message


def test_可流转状态与终态判定():
    """可流转列表顺序稳定且与流转表一致；退订是唯一的终态。"""
    assert next_statuses("active") == ["expired", "cancelled"]
    assert next_statuses("expired") == ["active", "cancelled"]
    assert next_statuses("cancelled") == []
    assert next_statuses("unknown") == []
    assert next_statuses(None) == []

    assert is_terminal("cancelled") is True
    assert is_terminal("active") is False
    assert is_terminal("expired") is False
    # 未知状态不判为终态，避免脏数据把状态永久锁死
    assert is_terminal("paused") is False


def test_非法流转提示带可选目标状态():
    """错误文案要说清现在能往哪走，管理侧才知道下一步该点哪个。"""
    with pytest.raises(OrderStatusTransitionError) as exc:
        validate_transition("cancelled", "active")
    assert exc.value.code == OrderErrorCode.INVALID_TRANSITION
    assert "退订" in exc.value.message
    assert "已结束流转" in exc.value.message

    with pytest.raises(OrderStatusTransitionError) as exc:
        validate_transition("active", "cancelled_")
    assert exc.value.code == OrderErrorCode.UNKNOWN_STATUS


def test_合法流转落库并返回变更结果(factory):
    """有效 → 过期：数据库状态被改写，返回体带原/目标状态与操作人。"""
    _add_order(factory)

    result = orders.change_order_status("O-9001", "expired", operator=" ops.team ", reason=" 服务期结束 ")

    assert result["order_id"] == "O-9001"
    assert result["from_status"] == "active"
    assert result["to_status"] == "expired"
    assert result["status_label"] == "过期"
    assert result["unchanged"] is False
    assert result["operator"] == "ops.team"
    assert result["reason"] == "服务期结束"
    assert result["allowed_statuses"] == ["active", "cancelled"]
    assert "续费" in result["message"]
    assert _stored_status(factory) == "expired"


def test_过期订单续费恢复有效(factory):
    """过期 → 有效：续费后原订单恢复服务，服务范围判定随即放行。"""
    now = time.time()
    _add_order(factory, status="expired")

    before = orders.evaluate_service_scope(_stored_order(factory), now=now)
    assert before["in_service"] is False
    assert before["reason_code"] == "order_inactive"

    orders.change_order_status("O-9001", "active", operator="billing")

    after = orders.evaluate_service_scope(_stored_order(factory), now=now)
    assert after["in_service"] is True
    assert after["reason_code"] == "in_service"


def test_退订后不可再流转(factory):
    """退订是终态：再次变更被拒，数据库状态保持不变。"""
    _add_order(factory)
    orders.change_order_status("O-9001", "cancelled")

    with pytest.raises(OrderStatusTransitionError) as exc:
        orders.change_order_status("O-9001", "active")
    assert exc.value.code == OrderErrorCode.INVALID_TRANSITION
    assert _stored_status(factory) == "cancelled"


def test_同状态变更按幂等处理(factory):
    """重复置为同一状态不报错也不写库，返回 unchanged 供调用方跳过后续动作。"""
    _add_order(factory)

    result = orders.change_order_status("O-9001", "active")

    assert result["unchanged"] is True
    assert result["from_status"] == "active"
    assert result["to_status"] == "active"
    assert "无需变更" in result["message"]
    assert _stored_status(factory) == "active"


def test_未知目标状态不写库(factory):
    """目标状态拼错时直接拒绝，订单不会被改成无法识别的状态。"""
    _add_order(factory)

    with pytest.raises(OrderStatusTransitionError) as exc:
        orders.change_order_status("O-9001", "pause")
    assert exc.value.code == OrderErrorCode.UNKNOWN_STATUS
    assert _stored_status(factory) == "active"


def test_订单号与归属校验(factory):
    """空订单号、不存在的订单、他人订单得到同一个「订单不存在」，不泄露归属。"""
    _add_order(factory, user_id="u-owner")

    for order_id, user_id in (("", None), ("O-missing", None), ("O-9001", "u-other")):
        with pytest.raises(OrderNotFoundError) as exc:
            orders.change_order_status(order_id, "expired", user_id=user_id)
        assert exc.value.code == OrderErrorCode.NOT_FOUND
        if order_id:
            # 他人订单与不存在的订单同文案，避免用订单号试探归属
            assert exc.value.to_payload()["error"] == "订单不存在"
        else:
            assert exc.value.to_payload()["error"] == "订单号不能为空"

    assert _stored_status(factory) == "active"

    # 归属正确时正常放行
    result = orders.change_order_status("O-9001", "expired", user_id="u-owner")
    assert result["to_status"] == "expired"
    assert _stored_status(factory) == "expired"


def test_状态流转说明(factory):
    """管理侧拿到的流转说明含当前状态、终态标记与可选项。"""
    _add_order(factory)
    flow = orders.describe_status_flow(_stored_order(factory))
    assert flow == {
        "order_id": "O-9001",
        "status": "active",
        "status_label": "有效",
        "known": True,
        "terminal": False,
        "allowed_statuses": ["expired", "cancelled"],
        "allowed_labels": ["过期", "退订"],
    }

    orders.change_order_status("O-9001", "cancelled")
    flow = orders.describe_status_flow(_stored_order(factory))
    assert flow["terminal"] is True
    assert flow["allowed_statuses"] == []

    empty = orders.describe_status_flow(None)
    assert empty["order_id"] is None
    assert empty["known"] is False
    assert empty["allowed_statuses"] == []


def test_状态标签覆盖全部取值():
    """每个合法状态都要有中文名，避免展示层出现空白。"""
    for status in OrderStatus:
        assert ORDER_STATUS_LABELS[status]
    assert set(ORDER_STATUS_LABELS) == set(OrderStatus)
