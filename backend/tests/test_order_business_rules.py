"""订单业务规则测试。

重点不是把每个函数再测一遍，而是验证几套规则凑在一起时不打架：
「现在能不能用」（服务范围）、「该不该提醒续费」（续费提示）、
「什么时候归档」（到期收敛）面对同一批订单必须给出一致的结论。

六组：

- 契约快照：状态 × 服务期十种组合下，服务范围与续费提示的结论逐项核对；
- 口径不变量：在期与提醒状态的对应关系、放行与原因码的对应关系；
- 生命周期：服务中 → 即将到期 → 紧急 → 已到期 → 归档 → 续费恢复；
- 汇总互证：续费提醒的计数、顺序、窗口归一要与逐条判定算出来的结果相同；
- 归档边界：只收敛「有效且服务期已结束」，重复执行幂等，归档后范围收紧；
- 并集与接口：多订单能力并集只取在期订单，四个订单接口与服务层结论一致，
  非管理员不能触发归档。

期望值在用例里按业务规则独立写出，不读实现的常量自证。
时间一律由用例传入 now，不依赖真实时钟。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import tickets as api_tickets
from backend.database import Base
from backend.models import Order, Product, Session as LoginSession, User
from backend.services import auth as auth_service
from backend.services import orders
from backend.tests.reference_api import ticket_service as ticket_service

DAY = 86400
NOW = 1_760_000_000.0  # 固定时刻，纯函数用例不随真实时钟波动

BASIC_FEATURES = ("基础导出", "单文件上传", "社区支持")
PRO_FEATURES = BASIC_FEATURES + ("批量导出", "高级报表", "优先客服支持", "API 接口")


def _order(
    order_id: str = "O-1",
    *,
    tier: str = "pro",
    status: str = "active",
    started_at: float = None,
    expires_at: float = None,
    user_id: str = "u-rule",
) -> dict:
    """构造一条订单记录，默认是服务中的专业版订单。"""
    return {
        "id": order_id,
        "user_id": user_id,
        "product_tier": tier,
        "status": status,
        "started_at": NOW - 10 * DAY if started_at is None else started_at,
        "expires_at": NOW + 100 * DAY if expires_at is None else expires_at,
    }


# ---------------------------------------------------------------------------
# 一、契约快照：状态 × 服务期
# ---------------------------------------------------------------------------

# 每行：(场景, 订单状态, 开始时间, 到期时间, 期望结论)
# 期望结论里的键与业务口径一一对应，服务范围与续费提示各断言一半。
CONTRACT_CASES = [
    (
        "服务中且离到期还远",
        "active",
        NOW - 10 * DAY,
        NOW + 100 * DAY,
        dict(in_service=True, allowed=True, reason="in_service",
             state="active", remind=False, urgency="none", action="none"),
    ),
    (
        "服务中且进入提醒窗口",
        "active",
        NOW - 110 * DAY,
        NOW + 20 * DAY,
        dict(in_service=True, allowed=True, reason="in_service",
             state="upcoming", remind=True, urgency="warning", action="renew"),
    ),
    (
        "服务中且不足一周",
        "active",
        NOW - 110 * DAY,
        NOW + 3 * DAY,
        dict(in_service=True, allowed=True, reason="in_service",
             state="upcoming", remind=True, urgency="critical", action="renew"),
    ),
    (
        "服务期已结束但状态仍有效",
        "active",
        NOW - 110 * DAY,
        NOW - DAY,
        dict(in_service=False, allowed=False, reason="expired",
             state="due", remind=True, urgency="critical", action="renew"),
    ),
    (
        "服务期尚未开始",
        "active",
        NOW + 5 * DAY,
        NOW + 100 * DAY,
        dict(in_service=False, allowed=False, reason="not_started",
             state="not_started", remind=False, urgency="none", action="wait"),
    ),
    (
        "已退订但服务期还在",
        "cancelled",
        NOW - 10 * DAY,
        NOW + 100 * DAY,
        dict(in_service=False, allowed=False, reason="order_inactive",
             state="blocked", remind=False, urgency="none", action="buy_new"),
    ),
    (
        "已归档为过期",
        "expired",
        NOW - 200 * DAY,
        NOW - 100 * DAY,
        dict(in_service=False, allowed=False, reason="order_inactive",
             state="due", remind=True, urgency="critical", action="renew"),
    ),
    (
        "起止时间颠倒",
        "active",
        NOW + 10 * DAY,
        NOW - 10 * DAY,
        dict(in_service=False, allowed=False, reason="period_unknown",
             state="unknown", remind=False, urgency="none", action="verify"),
    ),
    (
        "状态取值为历史脏数据",
        "paused",
        NOW - 10 * DAY,
        NOW + 100 * DAY,
        dict(in_service=False, allowed=False, reason="order_inactive",
             state="unknown", remind=False, urgency="none", action="verify"),
    ),
    (
        "状态值缺失",
        "",
        NOW - 10 * DAY,
        NOW + 100 * DAY,
        dict(in_service=False, allowed=False, reason="order_inactive",
             state="unknown", remind=False, urgency="none", action="verify"),
    ),
]


@pytest.mark.parametrize("name,status,started_at,expires_at,expected", CONTRACT_CASES,
                         ids=[case[0] for case in CONTRACT_CASES])
def test_状态与服务期组合的结论符合契约(name, status, started_at, expires_at, expected):
    order = _order(status=status, started_at=started_at, expires_at=expires_at)
    scope = orders.evaluate_service_scope(order, None, NOW)
    hint = orders.evaluate_renewal(order, NOW)

    assert scope["in_service"] is expected["in_service"], name
    assert scope["allowed"] is expected["allowed"], name
    assert scope["reason_code"] == expected["reason"], name

    assert hint["state"] == expected["state"], name
    assert hint["remind"] is expected["remind"], name
    assert hint["urgency"] == expected["urgency"], name
    assert hint["suggested_action"] == expected["action"], name


@pytest.mark.parametrize("name,status,started_at,expires_at,expected", CONTRACT_CASES,
                         ids=[case[0] for case in CONTRACT_CASES])
def test_在期结论与续费提醒状态互不矛盾(name, status, started_at, expires_at, expected):
    """两套口径必须能互相推导，否则前端会在「能用」的订单上弹续费窗口。"""
    order = _order(status=status, started_at=started_at, expires_at=expires_at)
    scope = orders.evaluate_service_scope(order, None, NOW)
    hint = orders.evaluate_renewal(order, NOW)

    # 在期等价于「没到期、也没被状态拦住」，对应提醒状态只有服务中与即将到期
    assert scope["in_service"] is (hint["state"] in ("active", "upcoming")), name
    # 放行一定在期内，且在期一定不因功能原因被拦（本例未传功能）
    assert not scope["allowed"] or scope["in_service"], name
    # 要打扰用户的两个状态必须是提醒态，反之亦然
    assert hint["remind"] is (hint["state"] in ("upcoming", "due")), name
    # 服务已停的订单不该给出续费这一个动作，退订与数据异常各有各的下一步
    if scope["reason_code"] in ("order_inactive", "period_unknown"):
        assert hint["suggested_action"] in ("buy_new", "verify", "renew"), name


def test_状态无法识别时不催续费():
    """状态为空或脏值时先核实订单状态，不能直接推「建议续费」。"""
    for status in ("paused", "", None):
        hint = orders.evaluate_renewal(_order(status=status), NOW)
        assert hint["state"] == "unknown", status
        assert hint["remind"] is False, status
        assert hint["suggested_action"] == "verify", status
        assert "核实" in hint["message"], status


def test_功能判定不改变服务期结论():
    """传了功能只影响放行与原因，服务期与提醒的那部分结论必须照旧。"""
    order = _order()
    plain = orders.evaluate_service_scope(order, None, NOW)
    with_feature = orders.evaluate_service_scope(order, "批量导出", NOW)

    assert with_feature["period"] == plain["period"]
    assert with_feature["in_service"] == plain["in_service"] is True
    assert with_feature["feature_included"] is True
    assert with_feature["allowed"] is True

    # 功能超出当前版本时只收紧放行与原因，不影响「订单在期内」这一事实
    basic = _order(tier="basic")
    exceeded = orders.evaluate_service_scope(basic, "批量导出", NOW)
    assert exceeded["in_service"] is True
    assert exceeded["allowed"] is False
    assert exceeded["reason_code"] == "feature_not_included"
    assert orders.evaluate_renewal(basic, NOW)["state"] == "active"


# ---------------------------------------------------------------------------
# 临时库：产品配置行 + 少量账号
# ---------------------------------------------------------------------------


def _patch_session(monkeypatch, factory, modules) -> None:
    for module in modules:
        monkeypatch.setattr(module, "SessionLocal", factory)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """只给订单服务用的临时库：产品配置行写全，订单由用例自己塞。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rules.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    _patch_session(monkeypatch, factory, (orders,))

    db = factory()
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

    yield factory
    engine.dispose()


def _add_order(factory, order_id: str, **overrides) -> None:
    payload = {
        "id": order_id,
        "user_id": "u-rule",
        "product_tier": "pro",
        "status": "active",
        "started_at": NOW - 10 * DAY,
        "expires_at": NOW + 100 * DAY,
    }
    payload.update(overrides)
    db = factory()
    try:
        db.add(Order(**payload))
        db.commit()
    finally:
        db.close()


def _rows(factory, ids) -> dict:
    db = factory()
    try:
        return {
            row.id: row
            for row in db.query(Order).filter(Order.id.in_(list(ids))).all()
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 二、生命周期：一条订单随时间推进
# ---------------------------------------------------------------------------


def test_订单随服务期推进的结论可复现(store):
    """同一批数据换一个 now，结论要按规则推进，而不是忽好忽坏。"""
    base = time.time()
    _add_order(
        store,
        "O-life",
        user_id="u-life",
        started_at=base - 30 * DAY,
        expires_at=base + 40 * DAY,
    )

    steps = [
        (0.0, "active", False, True, "none"),
        (15 * DAY, "upcoming", True, True, "warning"),
        (35 * DAY, "upcoming", True, True, "critical"),
        (41 * DAY, "due", True, False, "critical"),
    ]
    for offset, state, remind, in_service, urgency in steps:
        moment = base + offset
        scope = orders.describe_order("u-life", now=moment)
        hint = orders.evaluate_renewal(orders.get_order("u-life"), moment)

        assert scope["in_service"] is in_service, offset
        assert hint["state"] == state, offset
        assert hint["remind"] is remind, offset
        assert hint["urgency"] == urgency, offset
        # 剩余天数只在期内有意义，到期后一律归零，避免话术给出「还剩 0 天」
        assert (scope["period"]["days_remaining"] > 0) is in_service, offset


def test_归档后续费恢复服务(store):
    """先归档再续费：状态与时间要一起回位，任何一步都不能单独生效。"""
    base = time.time()
    _add_order(
        store,
        "O-back",
        user_id="u-back",
        started_at=base - 100 * DAY,
        expires_at=base - 10 * DAY,
    )

    before = orders.describe_order("u-back", now=base)
    assert before["in_service"] is False
    assert before["reason_code"] == "expired"
    assert orders.evaluate_renewal(orders.get_order("u-back"), base)["state"] == "due"

    archived = orders.expire_overdue_orders(user_id="u-back", now=base)
    assert archived["archived"] == ["O-back"]

    after = orders.describe_order("u-back", now=base)
    assert after["order_status"] == "expired"
    assert after["reason_code"] == "order_inactive"
    assert after["in_service"] is False
    # 归档只改状态，服务期时间属于合同事实，不能被归档动作改写
    assert after["period"]["expires_at"] == before["period"]["expires_at"]

    # 续费：状态回有效 + 服务期顺延，两步都做完才算续费成功
    orders.change_order_status("O-back", "active", operator="ops", reason="续费一年", now=base)
    db = store()
    try:
        db.query(Order).filter(Order.id == "O-back").update(
            {"expires_at": base + 365 * DAY}
        )
        db.commit()
    finally:
        db.close()

    renewed = orders.describe_order("u-back", now=base)
    assert renewed["order_status"] == "active"
    assert renewed["in_service"] is True
    assert renewed["allowed"] is True
    assert renewed["reason_code"] == "in_service"
    assert renewed["period"]["days_remaining"] == 365
    assert orders.evaluate_renewal(orders.get_order("u-back"), base)["state"] == "active"


# ---------------------------------------------------------------------------
# 三、续费提醒汇总与逐条判定
# ---------------------------------------------------------------------------


def test_提醒汇总的计数与顺序由逐条判定推出(store):
    base = time.time()
    _add_order(store, "O-far", user_id="u-hint", expires_at=base + 100 * DAY)
    _add_order(store, "O-cancel", user_id="u-hint", status="cancelled",
               expires_at=base + 50 * DAY)
    _add_order(store, "O-soon", user_id="u-hint", started_at=base - 110 * DAY,
               expires_at=base + 20 * DAY)
    _add_order(store, "O-over", user_id="u-hint", started_at=base - 110 * DAY,
               expires_at=base - 5 * DAY)

    summary = orders.list_renewal_hints("u-hint", now=base)
    # 顺序沿用取数口径：到期时间从晚到早
    assert [hint["order_id"] for hint in summary["hints"]] == [
        "O-far", "O-cancel", "O-soon", "O-over",
    ]
    assert [hint["state"] for hint in summary["hints"]] == [
        "active", "blocked", "upcoming", "due",
    ]
    assert summary["order_count"] == 4
    assert summary["due_count"] == 1
    assert summary["upcoming_count"] == 1
    assert summary["remind_count"] == 2
    assert summary["window_days"] == 30
    assert "1 个订单已到期" in summary["message"]

    # 汇总里的每一条都必须与逐条判定算出来的结果一致
    for row in orders.list_orders("u-hint"):
        expected = orders.evaluate_renewal(row, base)
        actual = next(h for h in summary["hints"] if h["order_id"] == row["id"])
        for key in ("state", "remind", "urgency", "suggested_action", "days_remaining"):
            assert actual[key] == expected[key], (row["id"], key)


def test_提醒窗口收窄只影响提前告知(store):
    """窗口调小后仍在紧急阈值内的照样提醒，已到期的条目不受影响。"""
    base = time.time()
    _add_order(store, "O-soon", user_id="u-win", started_at=base - 110 * DAY,
               expires_at=base + 20 * DAY)
    _add_order(store, "O-urgent", user_id="u-win", started_at=base - 115 * DAY,
               expires_at=base + 3 * DAY)
    _add_order(store, "O-over", user_id="u-win", started_at=base - 110 * DAY,
               expires_at=base - 5 * DAY)

    wide = orders.list_renewal_hints("u-win", now=base)
    narrow = orders.list_renewal_hints("u-win", now=base, window_days=0)

    assert wide["upcoming_count"] == 2
    assert narrow["upcoming_count"] == 1
    assert narrow["due_count"] == wide["due_count"] == 1
    assert narrow["window_days"] == 0
    states = {hint["order_id"]: hint["state"] for hint in narrow["hints"]}
    assert states["O-urgent"] == "upcoming"
    assert states["O-soon"] == "active"


def test_无订单时提醒汇总给空结论(store):
    summary = orders.list_renewal_hints("u-nobody", now=NOW)
    assert summary["has_order"] is False
    assert summary["order_count"] == 0
    assert summary["hints"] == []
    assert summary["message"] == "未查询到订单记录，暂无续费提示"


# ---------------------------------------------------------------------------
# 四、到期归档的边界
# ---------------------------------------------------------------------------


def test_归档只收敛有效且服务期已结束的订单(store):
    base = time.time()
    _add_order(store, "O-arch", user_id="u-arch", started_at=base - 100 * DAY,
               expires_at=base - 10 * DAY)
    _add_order(store, "O-cancel", user_id="u-arch", status="cancelled",
               started_at=base - 100 * DAY, expires_at=base - 10 * DAY)
    _add_order(store, "O-future", user_id="u-arch", started_at=base + 5 * DAY,
               expires_at=base + 100 * DAY)
    _add_order(store, "O-live", user_id="u-arch", expires_at=base + 100 * DAY)
    _add_order(store, "O-done", user_id="u-arch", status="expired",
               started_at=base - 200 * DAY, expires_at=base - 100 * DAY)

    result = orders.expire_overdue_orders(now=base)
    assert result["archived"] == ["O-arch"]
    assert result["scanned"] == 1
    assert result["has_more"] is False

    rows = _rows(store, ["O-arch", "O-cancel", "O-future", "O-live", "O-done"])
    assert rows["O-arch"].status == "expired"
    # 其余四种：退订是终态、未开始与在期不该归档、已归档的不重复动
    assert rows["O-cancel"].status == "cancelled"
    assert rows["O-future"].status == "active"
    assert rows["O-live"].status == "active"
    assert rows["O-done"].status == "expired"

    # 归档不改服务期，也不新增第二条状态变更
    again = orders.expire_overdue_orders(now=base)
    assert again["archived"] == []
    assert again["scanned"] == 0
    assert again["message"] == "没有需要归档的到期订单"
    assert _rows(store, ["O-arch"])["O-arch"].expires_at == base - 10 * DAY


def test_归档分批可续做(store):
    base = time.time()
    for index in range(3):
        _add_order(store, f"O-batch-{index}", user_id="u-batch",
                   started_at=base - 100 * DAY, expires_at=base - (10 + index) * DAY)

    first = orders.expire_overdue_orders(now=base, limit=2)
    assert len(first["archived"]) == 2
    assert first["remaining"] == 1
    assert first["has_more"] is True
    # 候选按到期时间从早到晚处理，先归档最久的那两条
    assert first["archived"] == ["O-batch-2", "O-batch-1"]

    second = orders.expire_overdue_orders(now=base, limit=2)
    assert second["archived"] == ["O-batch-0"]
    assert second["has_more"] is False


def test_归档后服务范围判定随之收紧(store):
    base = time.time()
    _add_order(store, "O-tight", user_id="u-tight", started_at=base - 100 * DAY,
               expires_at=base - 10 * DAY)

    before = orders.describe_order("u-tight", feature="批量导出", now=base)
    assert before["reason_code"] == "expired"
    assert before["allowed"] is False

    orders.expire_overdue_orders(user_id="u-tight", now=base)

    after = orders.describe_order("u-tight", feature="批量导出", now=base)
    assert after["order_status"] == "expired"
    assert after["reason_code"] == "order_inactive"
    assert after["allowed"] is False
    # 功能层结论不因归档改变：专业版仍然包含批量导出，只是这条订单不能用了
    assert after["feature"]["included"] is True


# ---------------------------------------------------------------------------
# 五、多订单的能力并集
# ---------------------------------------------------------------------------


def test_能力并集只取在期订单(store):
    base = time.time()
    _add_order(store, "O-mix-basic", user_id="u-mix", product_tier="basic",
               started_at=base - 10 * DAY, expires_at=base + 100 * DAY)
    _add_order(store, "O-mix-pro", user_id="u-mix", product_tier="pro",
               started_at=base - 100 * DAY, expires_at=base - 10 * DAY)

    view = orders.query_orders("u-mix", now=base)
    assert view["order_count"] == 2
    assert view["in_service_count"] == 1
    assert view["primary_order_id"] == "O-mix-basic"
    assert view["features"] == list(BASIC_FEATURES)

    verdict = orders.describe_feature_scope("u-mix", "批量导出", now=base)
    assert verdict["in_scope"] is False
    assert verdict["reason_code"] == "feature_not_included"
    # 已过期那条专业版仍被逐单明细标出，便于解释「为什么不算在范围内」
    assert verdict["in_service_count"] == 1
    assert {item["order_id"]: item["in_service"] for item in verdict["orders"]} == {
        "O-mix-basic": True,
        "O-mix-pro": False,
    }

    summary = orders.list_renewal_hints("u-mix", now=base)
    assert summary["due_count"] == 1
    # 在期但离到期还有 100 天，不该进提醒窗口
    assert summary["upcoming_count"] == 0
    assert summary["remind_count"] == 1


def test_并集覆盖时功能判定与服务期判定一致(store):
    base = time.time()
    _add_order(store, "O-pair-basic", user_id="u-pair", product_tier="basic",
               started_at=base - 10 * DAY, expires_at=base + 50 * DAY)
    _add_order(store, "O-pair-pro", user_id="u-pair", product_tier="pro",
               started_at=base - 10 * DAY, expires_at=base + 100 * DAY)

    verdict = orders.describe_feature_scope("u-pair", "批量导出", now=base)
    assert verdict["in_scope"] is True
    # 两条都在期时由到期更晚的那条覆盖
    assert verdict["covered_by"] == "O-pair-pro"
    assert verdict["feature"]["code"] == "export.batch"

    # 单条订单的判定也放行，两个口径不冲突
    single = orders.describe_order("u-pair", feature="批量导出", order_id="O-pair-pro", now=base)
    assert single["allowed"] is True
    assert single["reason_code"] == "in_service"

    # 指定基础版那条则收紧为不包含，说明单条判定与并集判定本就不是一回事
    limited = orders.describe_order("u-pair", feature="批量导出", order_id="O-pair-basic", now=base)
    assert limited["allowed"] is False
    assert limited["reason_code"] == "feature_not_included"


# ---------------------------------------------------------------------------
# 六、接口层：结论要与服务层一致
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 管理员与客户，挂载工单路由（订单接口都在这个路由下）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rules-api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    _patch_session(monkeypatch, factory, (api_tickets, ticket_service, auth_service, orders))

    now = time.time()
    db = factory()
    try:
        db.add_all(
            [
                User(id="u-admin", username="rule-admin", password_hash="x",
                     name="客服小张", role="admin"),
                User(id="u-cust", username="rule-cust", password_hash="x",
                     name="客户小李", role="customer"),
                LoginSession(token="tk-rule-admin", user_id="u-admin", username="rule-admin",
                             name="客服小张", role="admin", expires_at=now + 3600),
                LoginSession(token="tk-rule-cust", user_id="u-cust", username="rule-cust",
                             name="客户小李", role="customer", expires_at=now + 3600),
                Product(id="basic", name="基础版", features=list(BASIC_FEATURES)),
                Product(id="pro", name="专业版", features=list(PRO_FEATURES)),
                Order(id="O-api-live", user_id="u-cust", product_tier="pro", status="active",
                      started_at=now - 110 * DAY, expires_at=now + 20 * DAY),
                Order(id="O-api-old", user_id="u-cust", product_tier="pro", status="active",
                      started_at=now - 200 * DAY, expires_at=now - 3 * DAY),
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


CUST = {"Authorization": "Bearer tk-rule-cust"}
ADMIN = {"Authorization": "Bearer tk-rule-admin"}


def test_服务范围接口与续费接口结论一致(client):
    scope = client.get("/api/order/scope", params={"feature": "批量导出"}, headers=CUST)
    assert scope.status_code == 200
    body = scope.json()
    assert body["has_order"] is True
    assert body["order_id"] == "O-api-live"
    assert body["in_service"] is True
    assert body["allowed"] is True
    assert body["reason_code"] == "in_service"
    assert body["period"]["days_remaining"] == 20
    assert "剩余 20 天" in body["message"]

    renewal = client.get("/api/order/renewal", headers=CUST)
    assert renewal.status_code == 200
    summary = renewal.json()
    assert summary["order_count"] == 2
    assert summary["due_count"] == 1
    assert summary["upcoming_count"] == 1
    assert summary["remind_count"] == 2

    # 接口结论与服务层同参数下的结论一致，接口不额外解释规则
    service_hint = orders.list_renewal_hints("u-cust")
    assert summary["upcoming_count"] == service_hint["upcoming_count"]
    assert summary["due_count"] == service_hint["due_count"]


def test_指定订单的判定不受主订单影响(client):
    body = client.get(
        "/api/order/scope", params={"order_id": "O-api-old"}, headers=CUST
    ).json()
    assert body["order_id"] == "O-api-old"
    assert body["in_service"] is False
    assert body["reason_code"] == "expired"
    assert body["allowed"] is False

    view = client.get("/api/order/list", headers=CUST).json()
    assert view["in_service_count"] == 1
    assert view["primary_order_id"] == "O-api-live"
    statuses = {item["order_id"]: item["order_status"] for item in view["orders"]}
    assert statuses == {"O-api-live": "active", "O-api-old": "active"}


def test_功能判定接口跨订单给结论(client):
    body = client.get(
        "/api/order/feature-scope", params={"feature": "批量导出"}, headers=CUST
    ).json()
    assert body["in_scope"] is True
    assert body["covered_by"] == "O-api-live"
    assert body["feature"]["code"] == "export.batch"
    assert body["in_service_count"] == 1

    missing = client.get(
        "/api/order/feature-scope", params={"feature": "量子计算"}, headers=CUST
    ).json()
    assert missing["in_scope"] is False
    assert missing["reason_code"] == "feature_unknown"


def test_归档接口只允许管理员触发(client):
    anonymous = client.post("/api/order/expire")
    assert anonymous.status_code == 401
    assert anonymous.json()["error_code"] == "TICKET_UNAUTHORIZED"

    denied = client.post("/api/order/expire", headers=CUST)
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "TICKET_FORBIDDEN"

    done = client.post("/api/order/expire", headers=ADMIN)
    assert done.status_code == 200
    assert done.json()["archived"] == ["O-api-old"]

    # 归档后列表与范围判定的结论同步收紧
    view = client.get("/api/order/list", headers=CUST).json()
    statuses = {item["order_id"]: item["order_status"] for item in view["orders"]}
    assert statuses["O-api-old"] == "expired"
    assert view["in_service_count"] == 1

    scope = client.get(
        "/api/order/scope", params={"order_id": "O-api-old"}, headers=CUST
    ).json()
    assert scope["reason_code"] == "order_inactive"
    assert scope["allowed"] is False


def test_匿名访问订单接口的宽松与严格分工(client):
    # /order 面向页面展示，匿名给空订单，由前端引导登录
    loose = client.get("/api/order")
    assert loose.status_code == 200
    assert loose.json() == {"order": None}

    # 涉及权益结论的接口在未登录时必须明确拒绝，不能返回空结论冒充「没有订单」
    for path in ("/api/order/list", "/api/order/scope", "/api/order/feature-scope",
                 "/api/order/renewal"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error_code"] == "TICKET_UNAUTHORIZED", path
