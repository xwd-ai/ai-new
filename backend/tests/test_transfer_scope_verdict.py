"""转人工判定中「超出服务范围」与「无法核实」的区分测试。

订单侧的服务范围结论有多种原因码，落到转人工判定时只有两类含义：
1. 确有依据判定不在服务范围内（无订单、服务已到期、功能不在版本能力内），
   结论记为 out_of_scope；
2. 信息不足、暂时判不了（所购版本不可识别、起止时间不完整、功能名不可识别），
   结论记为 scope_unverified，不得写成「超出服务范围」。

测试覆盖判定函数与 HTTP 接口两层，重点断言两件事：判不了时不出现
out_of_scope 原因码与「超出服务范围」文案；判不了之外的情形结论保持不变。

测试使用独立的临时 SQLite 库，不污染真实 app.db。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import tickets as api_tickets
from backend.database import Base
from backend.models import Order, Product, Session, User
from backend.services import auth as auth_service
from backend.services import orders as orders_service
from backend.tests.reference_api import ticket_service as tickets

PRO_FEATURES = ["基础导出", "批量导出", "高级报表"]


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """只挂载工单路由的测试应用，预置管理员与一份专业版有效订单。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'scope_verdict.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    for module in (api_tickets, tickets, auth_service, orders_service):
        monkeypatch.setattr(module, "SessionLocal", test_session)

    now = time.time()
    db = test_session()
    try:
        db.add_all(
            [
                User(
                    id="u-admin",
                    username="admin",
                    password_hash="x",
                    name="客服小李",
                    role="admin",
                ),
                User(
                    id="u-pro",
                    username="pro",
                    password_hash="x",
                    name="专业版用户",
                    role="customer",
                ),
                Session(
                    token="tk-admin",
                    user_id="u-admin",
                    username="admin",
                    name="客服小李",
                    role="admin",
                    expires_at=now + 3600,
                ),
                Session(
                    token="tk-pro",
                    user_id="u-pro",
                    username="pro",
                    name="专业版用户",
                    role="customer",
                    expires_at=now + 3600,
                ),
                Product(id="pro", name="专业版", features=PRO_FEATURES),
                Order(
                    id="O-9001",
                    user_id="u-pro",
                    product_tier="pro",
                    status="active",
                    started_at=now - 86400,
                    expires_at=now + 86400 * 30,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.include_router(api_tickets.tickets_router, prefix="/api")
    yield TestClient(app), test_session
    engine.dispose()


ADMIN = {"Authorization": "Bearer tk-admin"}


# --------------------------------------------------------------------------
# 造数据辅助
# --------------------------------------------------------------------------


def _ticket(status: str = "open", description: str = "专业版导出失败") -> dict:
    return {
        "id": "T-3000",
        "status": status,
        "description": description,
        "tried_steps": "",
        "replies": [],
        "created_at": time.time(),
    }


def _order_scope(
    *,
    has_order: bool = True,
    in_service_count: int = 1,
    in_scope: bool = False,
    reason_code: str = "feature_unknown",
    reason: str = "无法识别要确认的功能「量子计算」",
) -> dict:
    """构造订单模块的多订单判定结论（只保留判定用到的字段）。"""
    return {
        "has_order": has_order,
        "in_scope": in_scope,
        "covered_by": "O-9001" if in_scope else None,
        "order_count": 1 if has_order else 0,
        "in_service_count": in_service_count,
        "reason_code": reason_code,
        "reason": reason,
    }


def _open_ticket(user_id: str, description: str) -> str:
    ticket, created = tickets.create_ticket(
        user_id=user_id,
        user_name="专业版用户",
        description=description,
        tried_steps="",
        evidence="",
    )
    assert created is True
    tickets.confirm_ticket(ticket["id"])
    return ticket["id"]


# --------------------------------------------------------------------------
# 判定函数：判不了与服务范围不符分开表达
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,reason",
    [
        ("feature_unknown", "无法识别要确认的功能「量子计算」"),
        ("tier_unknown", "无法识别所购版本，需要人工核实购买情况"),
        ("period_unknown", "订单起止时间不完整，无法判断服务期"),
    ],
)
def test_判不了的原因码不写成超出服务范围(code, reason):
    """版本、服务期、功能名不可识别时结论只能是待核实。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(),
        order_scope=_order_scope(reason_code=code, reason=reason),
        feature="量子计算",
    )

    assert decision["reason_codes"] == ["scope_unverified"]
    assert decision["should_transfer"] is True
    assert decision["suggested_status"] == tickets.STATUS_TRANSFERRED_HUMAN
    verdict = decision["basis"]["service_scope"]
    assert verdict["out_of_scope"] is False
    assert verdict["unverified"] is True
    assert verdict["code"] == "scope_unverified"
    assert verdict["in_service_count"] == 1
    assert "超出服务范围" not in decision["reason_text"]
    assert "无法自动核实服务范围" in decision["reason_text"]


def test_判不了时原因文案用待核实标签():
    """展示文案取 TRANSFER_REASON_LABELS 中的待核实标签，而非超范围标签。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(), order_scope=_order_scope(), feature="量子计算"
    )

    assert decision["reasons"][0]["code"] == "scope_unverified"
    assert decision["reasons"][0]["label"] == "服务范围待人工核实"
    assert decision["message"].startswith("建议转人工：服务范围待人工核实")


def test_功能不在版本能力内仍判超出服务范围():
    """功能名能识别但版本不含该功能，属于确有其据的超范围，结论不变。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(),
        order_scope=_order_scope(
            in_scope=False,
            reason_code="feature_not_included",
            reason="当前版本不包含该功能",
        ),
        feature="批量导出",
    )

    assert decision["reason_codes"] == ["out_of_scope"]
    assert decision["basis"]["service_scope"]["unverified"] is False
    assert decision["basis"]["service_scope"]["code"] == "feature_not_included"
    assert "超出服务范围" in decision["reason_text"]


def test_无订单与服务到期仍是超出服务范围():
    """缺少订单或服务已结束都是确定的结论，不进入待核实分支。"""
    no_order = tickets.evaluate_transfer_rule(
        _ticket(),
        order_scope=_order_scope(
            has_order=False,
            in_service_count=0,
            reason_code="no_order",
            reason="未查询到订单记录，无法判断服务范围",
        ),
    )
    expired = tickets.evaluate_transfer_rule(
        _ticket(),
        order_scope=_order_scope(
            in_service_count=0,
            reason_code="expired",
            reason="服务期已结束",
        ),
        feature="批量导出",
    )

    assert no_order["reason_codes"] == ["out_of_scope"]
    assert no_order["basis"]["service_scope"]["unverified"] is False
    assert expired["reason_codes"] == ["out_of_scope"]
    assert expired["basis"]["service_scope"]["unverified"] is False


def test_在服务范围内时不产生待核实原因():
    """功能被某条在期订单覆盖时既不算超范围，也不需要人工核实。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(),
        order_scope=_order_scope(in_scope=True, reason_code="in_scope", reason="在服务范围内"),
        feature="批量导出",
    )

    assert decision["should_transfer"] is False
    assert decision["reason_codes"] == []
    verdict = decision["basis"]["service_scope"]
    assert verdict["out_of_scope"] is False
    assert verdict["unverified"] is False


def test_判不了与用户诉求同时命中时两条原因并存():
    """用户主动要求排在第一，待核实作为第二条依据，顺序与优先级一致。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(),
        order_scope=_order_scope(),
        feature="量子计算",
        user_text="这个功能能转人工确认下吗",
    )

    assert decision["reason_codes"] == ["user_requested", "scope_unverified"]
    assert decision["reason_text"].index("用户主动要求转人工") < decision["reason_text"].index(
        "服务范围待人工核实"
    )


def test_单条订单视图的判定结论保持原样():
    """只给 order 时按单订单比对，功能对不上仍算超范围，且不出现待核实标记。"""
    single = {
        "has_order": True,
        "product_tier": "pro",
        "product_name": "专业版",
        "in_service": True,
        "features": PRO_FEATURES,
    }
    decision = tickets.evaluate_transfer_rule(
        _ticket(), order=single, feature="量子计算"
    )

    assert decision["reason_codes"] == ["out_of_scope"]
    assert "service_scope" not in decision["basis"]
    assert tickets._check_service_scope(single, "量子计算")["unverified"] is False


# --------------------------------------------------------------------------
# 接口层：判定、转人工与时间线落痕
# --------------------------------------------------------------------------


def test_判不了的功能名在判定接口返回待核实(api_client):
    """判定接口对无法识别的功能名给出待核实结论，文案不提超范围。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "量子计算模块支持吗")

    resp = client.get(
        f"/api/tickets/{ticket_id}/transfer-decision",
        params={"feature": "量子计算"},
        headers=ADMIN,
    )

    assert resp.status_code == 200
    decision = resp.json()["decision"]
    assert decision["reason_codes"] == ["scope_unverified"]
    assert decision["basis"]["service_scope"]["code"] == "scope_unverified"
    assert "超出服务范围" not in resp.json()["message"]


def test_可识别且覆盖的功能名不需要转人工(api_client):
    """专业版覆盖批量导出，判定接口不给出任何转人工依据。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "批量导出失败")

    resp = client.get(
        f"/api/tickets/{ticket_id}/transfer-decision",
        params={"feature": "批量导出"},
        headers=ADMIN,
    )

    assert resp.status_code == 200
    assert resp.json()["decision"]["should_transfer"] is False


def test_按规则转人工时原因写待核实而非超范围(api_client):
    """auto=true 转人工成功后，时间线里的原因与规则码与判定口径一致。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "量子计算模块支持吗")

    resp = client.post(
        "/api/tickets/transfer",
        json={"ticket_id": ticket_id, "feature": "量子计算", "auto": True},
        headers=ADMIN,
    )

    assert resp.status_code == 200
    body = resp.json()
    transfer = body["ticket"]["transfer"]
    assert transfer["reason_codes"] == ["scope_unverified"]
    assert "超出服务范围" not in transfer["reason"]
    assert "待人工核实" in transfer["reason"]
    assert body["ticket"]["status"] == tickets.STATUS_TRANSFERRED_HUMAN
    assert transfer["by_rule"] is True
