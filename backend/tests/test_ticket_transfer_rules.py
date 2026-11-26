"""转人工规则与接口测试。

覆盖三层能力：
1. 规则判定：用户主动要求、超出服务范围（无订单 / 服务到期 / 功能不含）、
   多次排查无效（已尝试步骤或人工回复达到阈值），以及终态工单不做判定；
2. 服务动作：转人工改写状态、写入原因与规则码、落状态时间线，
   非法来源状态由流转校验拒绝，缺省原因按规则原因、默认说明依次兜底；
3. 接口行为：权限（未登录 / 非管理员）、边界（工单不存在）、
   自动判定与显式转人工的分支，以及判定接口的只读语义。

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
from backend.models import Order, Product, Session, Ticket, User
from backend.models.ticket import TicketErrorCode, TicketTransitionError
from backend.services import auth as auth_service
from backend.services import orders as orders_service
from backend.tests.reference_api import ticket_service as tickets

PRO_FEATURES = ["批量导出", "团队协作", "工单支持"]


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """临时库 + 服务层 SessionLocal 替换。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'transfer.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(tickets, "SessionLocal", test_session)
    monkeypatch.setattr(orders_service, "SessionLocal", test_session)

    yield test_session

    engine.dispose()


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """只挂载工单路由的测试应用，预置管理员、普通用户与一份专业版有效订单。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'transfer_api.db'}",
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
                User(id="u-admin", username="admin", password_hash="x", name="客服小李", role="admin"),
                User(id="u-cust", username="cust", password_hash="x", name="客户小王", role="customer"),
                User(id="u-pro", username="pro", password_hash="x", name="专业版用户", role="customer"),
                Session(
                    token="tk-admin",
                    user_id="u-admin",
                    username="admin",
                    name="客服小李",
                    role="admin",
                    expires_at=now + 3600,
                ),
                Session(
                    token="tk-cust",
                    user_id="u-cust",
                    username="cust",
                    name="客户小王",
                    role="customer",
                    expires_at=now + 3600,
                ),
                Product(id="pro", name="专业版", features=PRO_FEATURES),
                Order(
                    id="O-1001",
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
CUSTOMER = {"Authorization": "Bearer tk-cust"}


# --------------------------------------------------------------------------
# 造数据辅助
# --------------------------------------------------------------------------


def _open_ticket(user_id: str, description: str, tried_steps: str = "") -> str:
    """建一张草稿并确认，得到一张待处理工单。"""
    ticket, created = tickets.create_ticket(
        user_id=user_id,
        user_name="测试用户",
        description=description,
        tried_steps=tried_steps,
        evidence="",
    )
    assert created is True
    tickets.confirm_ticket(ticket["id"])
    return ticket["id"]


def _order(
    *,
    has_order: bool = True,
    in_service: bool = True,
    features: list[str] | None = None,
    product_name: str = "专业版",
) -> dict:
    """构造订单视图，字段与订单查询服务的返回保持一致。"""
    if not has_order:
        return {"has_order": False, "message": "未查询到订单"}
    return {
        "has_order": True,
        "product_tier": "pro",
        "product_name": product_name,
        "in_service": in_service,
        "features": PRO_FEATURES if features is None else features,
    }


def _ticket(status: str = "open", description: str = "专业版导出失败", tried_steps: str = "") -> dict:
    """构造判定入参所需的工单视图。"""
    return {
        "id": "T-2000",
        "status": status,
        "description": description,
        "tried_steps": tried_steps,
        "replies": [],
        "created_at": time.time(),
    }


# --------------------------------------------------------------------------
# 用户主动要求转人工
# --------------------------------------------------------------------------


def test_识别用户转人工诉求并返回命中说法():
    """多种口语化说法都能识别，并回显命中的原文。"""
    assert tickets.detect_transfer_request("这个能转人工吗") == "转人工"
    assert tickets.detect_transfer_request("我要找客服") == "找客服"
    assert tickets.detect_transfer_request("麻烦人工客服处理一下") == "人工客服"
    assert tickets.detect_transfer_request("请联系客服升级处理") == "联系客服"


def test_普通问题描述不误判为转人工():
    """描述技术问题时不应被当成转人工诉求。"""
    assert tickets.detect_transfer_request("专业版导出失败，提示权限不足") == ""
    assert tickets.detect_transfer_request("导出的文件是空的") == ""


def test_空文本不产生转人工诉求():
    """空值与 None 都返回空串，不做任何推断。"""
    assert tickets.detect_transfer_request("") == ""
    assert tickets.detect_transfer_request(None) == ""


def test_用户主动要求优先级最高():
    """用户主动要求排在其他规则之前，无订单时也先给出用户诉求。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(), order=_order(has_order=False), user_text="帮我转人工"
    )

    assert decision["should_transfer"] is True
    assert decision["reason_codes"][0] == "user_requested"
    assert "转人工" in decision["basis"]["matched_keyword"]
    assert decision["reason_text"].startswith("用户主动要求转人工")


def test_工单描述中的转人工诉求也能命中():
    """用户没在当前消息里说，但建单描述里写了，同样算主动要求。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(description="导出失败，请转人工处理"), order=_order(), user_text=""
    )

    assert decision["reason_codes"] == ["user_requested"]


# --------------------------------------------------------------------------
# 超出服务范围
# --------------------------------------------------------------------------


def test_无订单视为超出服务范围():
    """查不到订单时无法核实服务范围，判定为超范围。"""
    decision = tickets.evaluate_transfer_rule(_ticket(), order=_order(has_order=False))

    assert decision["should_transfer"] is True
    assert decision["reason_codes"] == ["out_of_scope"]
    assert "未查询到有效订单" in decision["reason_text"]
    assert decision["basis"]["has_order"] is False


def test_服务到期视为超出服务范围():
    """订单存在但不在服务期内，同样转人工，并给出续费说明。"""
    decision = tickets.evaluate_transfer_rule(_ticket(), order=_order(in_service=False))

    assert decision["reason_codes"] == ["out_of_scope"]
    assert "服务已到期" in decision["reason_text"]
    assert decision["basis"]["in_service"] is False


def test_功能不在当前版本视为超出服务范围():
    """问题涉及的功能不在版本能力内时，说明中带上功能名与版本名。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(), order=_order(features=["单个导出"], product_name="基础版"), feature="批量导出"
    )

    assert decision["reason_codes"] == ["out_of_scope"]
    assert "基础版不包含该功能「批量导出」" in decision["reason_text"]
    assert decision["basis"]["feature"] == "批量导出"


def test_功能名称双向包含视为在范围内():
    """用户说「导出」而版本特性写作「批量导出」时仍算在范围内。"""
    decision = tickets.evaluate_transfer_rule(_ticket(), order=_order(), feature="导出")

    assert decision["should_transfer"] is False
    assert decision["reason_codes"] == []


# --------------------------------------------------------------------------
# 多次排查无效
# --------------------------------------------------------------------------


def test_已尝试步骤达到阈值判定多次排查无效():
    """步骤数达到阈值即视为自助手段已用尽。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(), order=_order(), tried_step_count=tickets.TRANSFER_TRIED_STEP_THRESHOLD
    )

    assert decision["reason_codes"] == ["ineffective_retries"]
    assert "已尝试 3 步" in decision["reason_text"]


def test_已尝试步骤不足阈值不判定():
    """差一步到阈值时仍不构成转人工理由。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(), order=_order(), tried_step_count=tickets.TRANSFER_TRIED_STEP_THRESHOLD - 1
    )

    assert decision["should_transfer"] is False


def test_人工回复达到阈值判定多次排查无效():
    """人工已回复多轮仍未闭环，视为反复沟通无效。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(), order=_order(), human_reply_count=tickets.TRANSFER_HUMAN_REPLY_THRESHOLD
    )

    assert decision["reason_codes"] == ["ineffective_retries"]
    assert "人工回复仍未闭环" in decision["reason_text"]


def test_两类排查无效只记一条原因码():
    """步骤与人工回复同时达标时不重复给出同一原因码。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(),
        order=_order(),
        tried_step_count=tickets.TRANSFER_TRIED_STEP_THRESHOLD,
        human_reply_count=tickets.TRANSFER_HUMAN_REPLY_THRESHOLD,
    )

    assert decision["reason_codes"] == ["ineffective_retries"]


def test_命中多条规则时原因按优先级排列():
    """用户诉求、超范围、排查无效可同时命中，输出顺序即业务优先级。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(),
        order=_order(in_service=False),
        user_text="转人工",
        tried_step_count=tickets.TRANSFER_TRIED_STEP_THRESHOLD,
    )

    assert decision["reason_codes"] == ["user_requested", "out_of_scope", "ineffective_retries"]
    assert decision["suggested_status"] == "transferred_human"


# --------------------------------------------------------------------------
# 无需判定与判定依据
# --------------------------------------------------------------------------


def test_服务范围内且排查轮次不足不建议转人工():
    """各项条件正常时明确给出不建议，并说明原因。"""
    decision = tickets.evaluate_transfer_rule(_ticket(), order=_order())

    assert decision["should_transfer"] is False
    assert decision["suggested_status"] == ""
    assert decision["message"].startswith("暂不需要转人工")
    assert decision["basis"]["tried_step_threshold"] == tickets.TRANSFER_TRIED_STEP_THRESHOLD
    assert decision["basis"]["human_reply_threshold"] == tickets.TRANSFER_HUMAN_REPLY_THRESHOLD


def test_已关闭工单不做转人工判定():
    """终态工单不允许流转到转人工，直接给出无需判定。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(status="closed"), order=_order(has_order=False), user_text="转人工"
    )

    assert decision["applicable"] is False
    assert decision["should_transfer"] is False
    assert decision["reasons"] == []
    assert "无需做转人工判定" in decision["message"]


def test_待确认草稿不做转人工判定():
    """待确认草稿要先确认才能跟进，判定阶段不给出转人工建议。"""
    decision = tickets.evaluate_transfer_rule(
        _ticket(status="pending_confirm"), order=_order(has_order=False)
    )

    assert decision["applicable"] is False
    assert decision["should_transfer"] is False


def test_判定依据回显阈值与关键字(db_env):
    """判定依据里带步骤数、人工回复数与命中关键字，便于解释结论。"""
    ticket_id = _open_ticket("u-pro", "批量导出失败", tried_steps="重启客户端 · 更换浏览器 · 清理缓存")
    data = tickets.get_ticket(ticket_id)

    decision = tickets.evaluate_transfer_rule(data, order=_order(), user_text="")

    # 描述里没有转人工诉求，但三步已尝试，命中排查无效
    assert decision["basis"]["tried_step_count"] == 3
    assert decision["basis"]["human_reply_count"] == 0
    assert decision["basis"]["matched_keyword"] == ""
    assert decision["reason_codes"] == ["ineffective_retries"]


def test_判定只读不改工单状态与回复(db_env):
    """判定只是建议，不写状态、不追加回复、不落时间线。"""
    ticket_id = _open_ticket("u-pro", "专业版导出失败")
    before = tickets.get_ticket(ticket_id)

    tickets.evaluate_transfer_rule(before, order=_order(has_order=False), user_text="转人工")

    after = tickets.get_ticket(ticket_id)
    assert after["status"] == "open"
    assert after["replies"] == before["replies"]
    assert len(after["status_timeline"]) == len(before["status_timeline"])


# --------------------------------------------------------------------------
# 转人工服务动作
# --------------------------------------------------------------------------


def test_转人工改写状态并写入原因与时间线(db_env):
    """转人工后状态、回复、时间线、责任人四处同时落痕。"""
    ticket_id = _open_ticket("u-pro", "专业版导出失败")
    data = tickets.get_ticket(ticket_id)
    decision = tickets.evaluate_transfer_rule(data, order=_order(in_service=False))

    result = tickets.transfer_to_human(ticket_id, operator="客服小李", decision=decision)

    assert result["status"] == "transferred_human"
    assert result["transfer"]["from_status"] == "open"
    assert result["transfer"]["to_status"] == "transferred_human"
    assert result["transfer"]["operator"] == "客服小李"
    assert result["transfer"]["by_rule"] is True

    reply = result["replies"][-1]
    assert reply["replier"] == "system"
    assert reply["kind"] == "transfer"
    assert reply["operator"] == "客服小李"
    assert reply["reason_codes"] == ["out_of_scope"]
    assert reply["content"].startswith("转人工：")

    detail = tickets.get_ticket(ticket_id)
    assert detail["status"] == "transferred_human"
    assert detail["status_timeline"][-1]["to_status"] == "transferred_human"
    assert detail["status_timeline"][-1]["operator"] == "客服小李"
    assert detail["reply_timeline"]["transfer_count"] == 1


def test_转人工原因缺省时回填规则原因(db_env):
    """未填写原因时不写空说明，直接用判定结论作为原因。"""
    ticket_id = _open_ticket("u-pro", "专业版导出失败")
    decision = tickets.evaluate_transfer_rule(tickets.get_ticket(ticket_id), order=_order(has_order=False))

    result = tickets.transfer_to_human(ticket_id, reason="", decision=decision)

    assert result["transfer"]["reason"] == decision["reason_text"]
    assert "未查询到有效订单" in result["replies"][-1]["reason"]


def test_转人工无原因时使用默认说明(db_env):
    """既没填原因也没有判定结果时，用默认说明兜底，保证可追溯。"""
    ticket_id = _open_ticket("u-pro", "专业版导出失败")

    result = tickets.transfer_to_human(ticket_id)

    assert result["transfer"]["reason"] == tickets.DEFAULT_TRANSFER_REASON
    assert result["transfer"]["by_rule"] is False
    assert result["replies"][-1]["content"] == f"转人工：{tickets.DEFAULT_TRANSFER_REASON}"


def test_显式原因优先于规则原因(db_env):
    """客服填写的原因优先级最高，规则结论只作为补充依据。"""
    ticket_id = _open_ticket("u-pro", "专业版导出失败")
    decision = tickets.evaluate_transfer_rule(
        tickets.get_ticket(ticket_id), order=_order(has_order=False)
    )

    result = tickets.transfer_to_human(
        ticket_id, reason="客户电话沟通后要求工程师介入", decision=decision
    )

    assert result["transfer"]["reason"] == "客户电话沟通后要求工程师介入"
    assert result["transfer"]["reason_codes"] == ["out_of_scope"]


def test_转人工原因压缩多余空白(db_env):
    """原因文本做空白归一，避免时间线里出现换行与连续空格。"""
    ticket_id = _open_ticket("u-pro", "专业版导出失败")

    result = tickets.transfer_to_human(ticket_id, reason="  客户   要求\n工程师介入 ")

    assert result["transfer"]["reason"] == "客户 要求 工程师介入"


def test_已关闭工单不可转人工(db_env):
    """已终结的工单不再接受转人工，由状态流转校验拒绝。"""
    ticket_id = _open_ticket("u-pro", "专业版导出失败")
    tickets.update_status(ticket_id, "closed")

    with pytest.raises(TicketTransitionError):
        tickets.transfer_to_human(ticket_id, reason="事后补转")


def test_待确认草稿不可转人工(db_env):
    """草稿必须先确认，不能跳过确认直接转人工。"""
    ticket, _ = tickets.create_ticket(
        user_id="u-pro",
        user_name="测试用户",
        description="专业版导出失败",
        tried_steps="",
        evidence="",
    )

    with pytest.raises(TicketTransitionError):
        tickets.transfer_to_human(ticket["id"], reason="直接转人工")


def test_转人工不存在的工单返回空(db_env):
    """工单不存在返回 None，由接口层转成「工单不存在」。"""
    assert tickets.transfer_to_human("T-不存在", reason="测试") is None


# --------------------------------------------------------------------------
# 转人工接口
# --------------------------------------------------------------------------


def test_接口未登录不可转人工(api_client):
    """未携带登录态时，转人工与判定接口都返回未登录。"""
    client, _ = api_client

    transfer = client.post("/api/tickets/transfer", json={"ticket_id": "T-2000"}).json()
    assert transfer["error"] == "未登录"
    assert transfer["error_code"] == TicketErrorCode.UNAUTHORIZED

    decision = client.get("/api/tickets/T-2000/transfer-decision").json()
    assert decision["error"] == "未登录"
    assert decision["error_code"] == TicketErrorCode.UNAUTHORIZED


def test_接口普通用户不可转人工(api_client):
    """转人工是客服动作，普通用户调用返回无权限。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-cust", "专业版导出失败")

    resp = client.post(
        "/api/tickets/transfer",
        json={"ticket_id": ticket_id, "reason": "自行转人工"},
        headers=CUSTOMER,
    ).json()

    assert resp["error"] == "无权限"
    assert resp["error_code"] == TicketErrorCode.FORBIDDEN


def test_接口转人工不存在的工单(api_client):
    """管理员转一张不存在的工单，返回明确的工单不存在。"""
    client, _ = api_client

    resp = client.post("/api/tickets/transfer", json={"ticket_id": "T-2000"}, headers=ADMIN).json()

    assert resp["error"] == "工单不存在"
    assert resp["error_code"] == TicketErrorCode.NOT_FOUND


def test_接口按规则自动转人工(api_client):
    """auto=true 且命中规则时执行转人工，响应回显命中规则与原因。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "专业版导出失败")

    resp = client.post(
        "/api/tickets/transfer",
        json={"ticket_id": ticket_id, "user_text": "这个能转人工吗", "auto": True},
        headers=ADMIN,
    ).json()

    assert resp["decision"]["should_transfer"] is True
    assert resp["ticket"]["status"] == "transferred_human"
    assert resp["ticket"]["transfer"]["by_rule"] is True
    assert resp["ticket"]["transfer"]["reason_codes"] == ["user_requested"]
    assert "命中规则：user_requested" in resp["message"]

    detail = client.get(f"/api/tickets/{ticket_id}", headers=ADMIN).json()["ticket"]
    assert detail["status"] == "transferred_human"
    assert detail["reply_timeline"]["transfer_count"] == 1


def test_接口规则未命中时拒绝自动转人工(api_client):
    """仍在服务范围内、排查也未达阈值时，auto=true 不会误转。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "专业版导出失败")

    resp = client.post(
        "/api/tickets/transfer",
        json={"ticket_id": ticket_id, "auto": True},
        headers=ADMIN,
    ).json()

    assert resp["error_code"] == TicketErrorCode.TRANSFER_RULE_NOT_MATCHED
    assert resp["error_detail"]["decision"]["should_transfer"] is False
    assert "未命中转人工规则" in resp["error"]
    # 拒绝时不产生任何副作用
    detail = client.get(f"/api/tickets/{ticket_id}", headers=ADMIN).json()["ticket"]
    assert detail["status"] == "open"
    assert detail["replies"] == []


def test_接口显式原因可直接转人工(api_client):
    """auto=false 时管理员可填写原因直接转人工，判定结论仅作参考。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "专业版导出失败")

    resp = client.post(
        "/api/tickets/transfer",
        json={"ticket_id": ticket_id, "reason": "客户来电要求工程师介入"},
        headers=ADMIN,
    ).json()

    transfer = resp["ticket"]["transfer"]
    assert resp["ticket"]["status"] == "transferred_human"
    assert transfer["reason"] == "客户来电要求工程师介入"
    assert transfer["by_rule"] is False
    assert "已转人工，原因：客户来电要求工程师介入" in resp["message"]


def test_接口转人工已关闭工单被拒绝(api_client):
    """已关闭的工单再转人工，返回流转校验的错误说明而非静默成功。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "专业版导出失败")
    client.post("/api/tickets/status", json={"ticket_id": ticket_id, "status": "closed"}, headers=ADMIN)

    resp = client.post(
        "/api/tickets/transfer",
        json={"ticket_id": ticket_id, "reason": "补转"},
        headers=ADMIN,
    ).json()

    assert "error" in resp
    assert "closed" in resp["error"]


# --------------------------------------------------------------------------
# 判定接口
# --------------------------------------------------------------------------


def test_判定接口只读返回建议(api_client):
    """判定接口给出建议与依据，但不改变工单状态与回复。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "专业版导出失败")

    resp = client.get(
        f"/api/tickets/{ticket_id}/transfer-decision",
        params={"user_text": "找了半天还是不行，转人工吧", "feature": "批量导出"},
        headers=ADMIN,
    ).json()

    assert resp["decision"]["should_transfer"] is True
    assert resp["decision"]["suggested_status"] == "transferred_human"
    assert "建议转人工" in resp["message"]
    assert resp["decision"]["basis"]["has_order"] is True
    assert resp["decision"]["basis"]["in_service"] is True

    detail = client.get(f"/api/tickets/{ticket_id}", headers=ADMIN).json()["ticket"]
    assert detail["status"] == "open"
    assert detail["replies"] == []


def test_判定接口普通用户越权被拒绝(api_client):
    """普通用户只能判定本人工单。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-pro", "专业版导出失败")

    resp = client.get(f"/api/tickets/{ticket_id}/transfer-decision", headers=CUSTOMER).json()

    assert resp["error"] == "无权限"
    assert resp["error_code"] == TicketErrorCode.FORBIDDEN


def test_判定接口本人可判定(api_client):
    """普通用户判定本人工单不受限，结论按本人的服务范围给出。"""
    client, _ = api_client
    ticket_id = _open_ticket("u-cust", "专业版导出失败")

    resp = client.get(f"/api/tickets/{ticket_id}/transfer-decision", headers=CUSTOMER).json()

    # 该用户没有订单记录，按超范围给出建议
    assert resp["decision"]["reason_codes"] == ["out_of_scope"]
    assert resp["decision"]["basis"]["has_order"] is False


def test_判定接口工单不存在(api_client):
    """不存在的工单不给出任何判定结论。"""
    client, _ = api_client

    resp = client.get("/api/tickets/T-2000/transfer-decision", headers=ADMIN).json()

    assert resp["error"] == "工单不存在"
    assert resp["error_code"] == TicketErrorCode.NOT_FOUND
