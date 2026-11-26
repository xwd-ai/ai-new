"""三模块全链路集成测试：登录 → 查订单 → 建单 → 确认 → 转人工 → 详情。

与单模块用例的分工：那些用例直接构造内部状态验证某个函数或某个接口，
本文件只从 HTTP 入口出发，用真实登录换到的 JWT 一路走完客户与客服的完整
处理链路，验证三个模块接起来之后的实际行为是否自洽：

1. 认证签发的令牌能被订单与工单接口直接使用，身份、角色与数据归属一致；
2. 订单侧的服务范围结论会真的影响工单侧的转人工判定，而不是各算各的；
3. 建单、确认、转人工的每一步都在同一张工单上留下可追溯的痕迹，
   状态时间线的起止状态首尾相接，操作人对应实际操作者；
4. 链路上的权限边界仍然有效：匿名 401、越权 403、普通用户不能执行客服动作。

全部用例使用独立临时 SQLite，不读写真实的 app.db；密码成本降到最低档，
只为缩短用例耗时，不改变校验逻辑。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import tickets as api_tickets
from backend.tests.reference_api.auth import auth_router
from backend.database import Base
from backend.models import Order, Product, User
from backend.services import auth as auth_service
from backend.services import orders as orders_service
from backend.tests.reference_api import ticket_service as ticket_service

ADMIN_USERNAME = "flow-admin"
ADMIN_NAME = "客服小张"
CUSTOMER_USERNAME = "flow-cust"
CUSTOMER_NAME = "客户李女士"
NEWBIE_USERNAME = "flow-newbie"
NEWBIE_NAME = "客户赵先生"
PASSWORD = "Flow12345"

CUSTOMER_ID = "u-flow-cust"
NEWBIE_ID = "u-flow-newbie"

PRO_TIER = "pro"
BASIC_TIER = "basic"
# 专业版独有、基础版没有的功能，用于构造「在范围内」与「不在范围内」两种结论
PRO_ONLY_FEATURE = "批量导出"

# 建单描述刻意避开「转人工」等关键词：是否转人工由用例显式传入的用户消息决定，
# 不让描述里的字样干扰判定结果；已尝试步骤只留两步，低于转人工的排查轮次阈值，
# 这样未命中服务范围、也未达到无效排查阈值时，唯一的原因只剩用户主动要求
DESCRIPTION = "专业版导出报表一直失败，重装客户端也没有恢复"
TRIED_STEPS = "重启软件 · 更换浏览器"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """固定签名密钥、去掉时钟余量、压低密码成本，避免用例受本机环境影响。"""
    monkeypatch.setenv(auth_service.JWT_SECRET_ENV, "unit-test-full-flow-secret-0123456789")
    monkeypatch.delenv(auth_service.JWT_LEEWAY_ENV, raising=False)
    monkeypatch.setenv("BCRYPT_COST", "6")


def _seed(session_factory):
    """一个客服、一个持有在期专业版的客户、一个没有订单的客户。"""
    now = time.time()
    admin = User(id="u-flow-admin", username=ADMIN_USERNAME, name=ADMIN_NAME, role="admin")
    admin.set_password(PASSWORD)
    customer = User(id=CUSTOMER_ID, username=CUSTOMER_USERNAME, name=CUSTOMER_NAME, role="customer")
    customer.set_password(PASSWORD)
    newbie = User(id=NEWBIE_ID, username=NEWBIE_USERNAME, name=NEWBIE_NAME, role="customer")
    newbie.set_password(PASSWORD)

    db = session_factory()
    try:
        db.add_all(
            [
                admin,
                customer,
                newbie,
                Product(id=PRO_TIER, name="专业版", features=["导出", PRO_ONLY_FEATURE]),
                Product(id=BASIC_TIER, name="基础版", features=["导出"]),
                Order(
                    id="O-FLOW-1",
                    user_id=CUSTOMER_ID,
                    product_tier=PRO_TIER,
                    status="active",
                    started_at=now - 86400,
                    expires_at=now + 86400 * 30,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def flow(tmp_path, monkeypatch):
    """临时库 + 认证/工单（含订单）两组路由，即三模块对外的全部入口。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'flow.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, ticket_service, auth_service, orders_service):
        monkeypatch.setattr(module, "SessionLocal", session_factory)
    _seed(session_factory)

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(api_tickets.tickets_router, prefix="/api")
    with TestClient(app) as client:
        yield client
    engine.dispose()


def _login(client, username: str, password: str = PASSWORD) -> dict:
    """走真实登录接口换令牌，返回可直接使用的请求头。"""
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"], body
    assert body["user"]["username"] == username
    return {"Authorization": f"Bearer {body['token']}"}


def _error_code(response) -> str:
    """取业务错误码，顺带守住「错误体必须带 error 与 error_code」。"""
    body = response.json()
    assert body.get("error") and body.get("error_code"), body
    return body["error_code"]


def _create_and_confirm(
    client, headers: dict, description: str = DESCRIPTION, tried_steps: str = TRIED_STEPS
) -> str:
    """建单并确认，返回正式进入待处理的工单号。

    tried_steps 可覆盖：已尝试步骤数量本身也是转人工的判定依据，
    需要构造「只因服务范围外而转人工」的场景时必须把它留空。
    """
    created = client.post(
        "/api/tickets",
        json={"description": description, "tried_steps": tried_steps, "evidence": "导出日志报 500"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["created"] is True, body
    ticket_id = body["ticket"]["id"]
    assert body["ticket"]["status"] == "pending_confirm"

    confirmed = client.post("/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["ticket"]["status"] == "open"
    return ticket_id


# ---------------------------------------------------------------------------
# 主链路：客户自助 → 客服接手
# ---------------------------------------------------------------------------


def test_客户从登录到转人工的完整链路(flow):
    """一条链路走完登录、查订单、建单、确认、判定、转人工与详情回看。"""
    customer = _login(flow, CUSTOMER_USERNAME)
    admin = _login(flow, ADMIN_USERNAME)

    # 一、登录身份：令牌直接可用，角色与账号一致
    me = flow.get("/api/me", headers=customer)
    assert me.status_code == 200
    assert me.json()["user"]["username"] == CUSTOMER_USERNAME
    assert me.json()["user"]["role"] == "customer"

    # 二、订单：主订单在期，专业版能力覆盖本次问题涉及的功能
    order = flow.get("/api/order", headers=customer).json()["order"]
    assert order["has_order"] is True
    assert order["order_id"] == "O-FLOW-1"
    assert order["product_tier"] == PRO_TIER
    assert order["in_service"] is True
    assert PRO_ONLY_FEATURE in order["features"]

    listed = flow.get("/api/order/list", headers=customer).json()
    assert listed["order_count"] == 1
    assert listed["in_service_count"] == 1
    assert listed["primary_order_id"] == "O-FLOW-1"
    assert listed["orders"][0]["order_id"] == "O-FLOW-1"

    single_scope = flow.get(
        "/api/order/scope", params={"feature": PRO_ONLY_FEATURE}, headers=customer
    ).json()
    assert single_scope["in_service"] is True
    assert single_scope["allowed"] is True
    assert single_scope["feature"]["included"] is True
    assert single_scope["feature"]["name"] == PRO_ONLY_FEATURE

    feature_scope = flow.get(
        "/api/order/feature-scope", params={"feature": PRO_ONLY_FEATURE}, headers=customer
    ).json()
    assert feature_scope["in_scope"] is True
    assert feature_scope["reason_code"] == "in_scope"
    assert feature_scope["covered_by"] == "O-FLOW-1"
    assert feature_scope["in_service_count"] == 1

    # 三、建单：草稿挂在登录账号名下，归属不接受外部指定
    created = flow.post(
        "/api/tickets",
        json={
            "description": DESCRIPTION,
            "tried_steps": TRIED_STEPS,
            "evidence": "导出日志报 500",
            "user_id": "u-flow-admin",
        },
        headers=customer,
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    ticket_id = payload["ticket"]["id"]
    assert payload["created"] is True
    assert payload["ticket"]["status"] == "pending_confirm"
    assert payload["ticket"]["user_id"] == CUSTOMER_ID
    assert payload["identity"]["id"] == CUSTOMER_ID
    assert payload["identity"]["role"] == "customer"

    # 四、待确认列表能看到自己的草稿，确认后才进入待处理
    pending = flow.get("/api/tickets/pending", headers=customer).json()
    assert [item["id"] for item in pending["tickets"]] == [ticket_id]
    assert pending["tickets"][0]["expired"] is False
    assert pending["tickets"][0]["remaining_seconds"] > 0

    confirmed = flow.post(
        "/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=customer
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["ticket"]["status"] == "open"
    assert confirmed.json()["identity"]["name"] == CUSTOMER_NAME

    # 五、判定：问题在服务范围内，仅因用户明确要求才建议转人工
    decision = flow.get(
        f"/api/tickets/{ticket_id}/transfer-decision",
        params={"user_text": "试了好几次都不行，麻烦帮我转人工", "feature": PRO_ONLY_FEATURE},
        headers=customer,
    ).json()["decision"]
    assert decision["applicable"] is True
    assert decision["should_transfer"] is True
    assert decision["reason_codes"] == ["user_requested"]
    assert decision["basis"]["service_scope"]["out_of_scope"] is False
    assert decision["basis"]["service_scope"]["covered_by"] == "O-FLOW-1"

    # 判定只读：看过判定结论，工单状态与回复都不应变化
    untouched = flow.get(f"/api/tickets/{ticket_id}", headers=customer).json()["ticket"]
    assert untouched["status"] == "open"
    assert untouched["reply_timeline"]["human_count"] == 0

    # 六、转人工：客服接手后状态流转，原因与命中规则一并落痕
    transferred = flow.post(
        "/api/tickets/transfer",
        json={
            "ticket_id": ticket_id,
            "user_text": "试了好几次都不行，麻烦帮我转人工",
            "feature": PRO_ONLY_FEATURE,
        },
        headers=admin,
    )
    assert transferred.status_code == 200, transferred.text
    transfer = transferred.json()["ticket"]
    assert transfer["status"] == "transferred_human"
    assert transfer["transfer"]["operator"] == ADMIN_NAME
    assert transfer["transfer"]["from_status"] == "open"
    assert transfer["transfer"]["reason_codes"] == ["user_requested"]
    assert transfer["transfer"]["by_rule"] is True
    assert transfer["transfer"]["reason"]
    assert transferred.json()["decision"]["should_transfer"] is True

    # 七、详情：状态时间线首尾相接，操作人对应实际操作者
    detail = flow.get(f"/api/tickets/{ticket_id}", headers=customer)
    assert detail.status_code == 200
    ticket = detail.json()["ticket"]
    assert ticket["status"] == "transferred_human"
    timeline = [(log["from_status"], log["to_status"], log["operator"]) for log in ticket["status_timeline"]]
    assert timeline == [
        ("", "pending_confirm", CUSTOMER_NAME),
        ("pending_confirm", "open", CUSTOMER_NAME),
        ("open", "transferred_human", ADMIN_NAME),
    ]
    # 转人工说明由系统写入，不计入人工回复，客服接手后仍需自己回复
    assert ticket["reply_timeline"]["human_count"] == 0
    assert ticket["reply_timeline"]["system_count"] == 1
    assert ticket["detail_summary"]["status_chain"] == [
        "pending_confirm",
        "open",
        "transferred_human",
    ]
    assert ticket["detail_summary"]["transfer_count"] == 1


def test_无订单客户的问题直接命中服务范围外(flow):
    """没有订单的客户：查询结论为空、判定命中超范围、自动转人工可执行。"""
    newbie = _login(flow, NEWBIE_USERNAME)

    order = flow.get("/api/order", headers=newbie).json()["order"]
    assert order["has_order"] is False
    assert order["message"]

    feature_scope = flow.get(
        "/api/order/feature-scope", params={"feature": PRO_ONLY_FEATURE}, headers=newbie
    ).json()
    assert feature_scope["in_scope"] is False
    assert feature_scope["reason_code"] == "no_order"
    assert feature_scope["order_count"] == 0

    ticket_id = _create_and_confirm(
        flow, newbie, description="导出功能打不开，换了电脑也一样", tried_steps=""
    )

    decision = flow.get(
        f"/api/tickets/{ticket_id}/transfer-decision",
        params={"feature": PRO_ONLY_FEATURE},
        headers=newbie,
    ).json()["decision"]
    assert decision["should_transfer"] is True
    assert decision["reason_codes"] == ["out_of_scope"]
    assert decision["basis"]["has_order"] is False
    assert decision["basis"]["service_scope"]["code"] == "no_order"

    # auto=true：未命中规则会拒绝，这里应当命中并直接转出去，原因由判定回填
    admin = _login(flow, ADMIN_USERNAME)
    transferred = flow.post(
        "/api/tickets/transfer",
        json={"ticket_id": ticket_id, "feature": PRO_ONLY_FEATURE, "auto": True},
        headers=admin,
    )
    assert transferred.status_code == 200, transferred.text
    body = transferred.json()
    assert body["ticket"]["status"] == "transferred_human"
    assert body["ticket"]["transfer"]["reason_codes"] == ["out_of_scope"]
    assert body["ticket"]["transfer"]["reason"]

    detail = flow.get(f"/api/tickets/{ticket_id}", headers=admin).json()["ticket"]
    assert detail["user_id"] == NEWBIE_ID
    assert [log["to_status"] for log in detail["status_timeline"]] == [
        "pending_confirm",
        "open",
        "transferred_human",
    ]


# ---------------------------------------------------------------------------
# 链路一致性：幂等、数据范围与权限
# ---------------------------------------------------------------------------


def test_重复建单在链路里复用未解决工单(flow):
    """同一问题再提一次不会多出工单，后续确认与转人工仍落在原单上。"""
    customer = _login(flow, CUSTOMER_USERNAME)

    first = flow.post("/api/tickets", json={"description": DESCRIPTION}, headers=customer).json()
    second = flow.post("/api/tickets", json={"description": DESCRIPTION}, headers=customer).json()

    assert first["created"] is True
    assert second["created"] is False
    assert second["ticket"]["id"] == first["ticket"]["id"]
    assert second["ticket"]["status"] == "pending_confirm"

    pending = flow.get("/api/tickets/pending", headers=customer).json()
    assert pending["total"] == 1

    # 确认后同一问题再来一次，仍未解决，继续复用
    ticket_id = first["ticket"]["id"]
    flow.post("/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=customer)
    third = flow.post("/api/tickets", json={"description": DESCRIPTION}, headers=customer).json()
    assert third["created"] is False
    assert third["ticket"]["id"] == ticket_id
    assert third["ticket"]["status"] == "open"


def test_链路上的权限边界依旧生效(flow):
    """普通用户只能碰自己的单，客服动作只有管理员能做，匿名一律拒绝。"""
    customer = _login(flow, CUSTOMER_USERNAME)
    newbie = _login(flow, NEWBIE_USERNAME)
    ticket_id = _create_and_confirm(flow, customer)

    # 他人查看、判定、确认、取消都被拒绝，且错误详情指回单主
    others_detail = flow.get(f"/api/tickets/{ticket_id}", headers=newbie)
    assert others_detail.status_code == 403
    assert _error_code(others_detail) == "TICKET_FORBIDDEN"
    assert others_detail.json()["error_detail"]["ticket_id"] == ticket_id
    assert others_detail.json()["error_detail"]["owner_id"] == CUSTOMER_ID

    others_decision = flow.get(f"/api/tickets/{ticket_id}/transfer-decision", headers=newbie)
    assert others_decision.status_code == 403
    assert _error_code(others_decision) == "TICKET_FORBIDDEN"

    others_confirm = flow.post(
        "/api/tickets/confirm", json={"ticket_id": ticket_id}, headers=newbie
    )
    assert others_confirm.status_code == 403
    assert _error_code(others_confirm) == "TICKET_FORBIDDEN"

    # 客服动作只对管理员开放：普通用户 403，匿名 401
    customer_transfer = flow.post(
        "/api/tickets/transfer", json={"ticket_id": ticket_id, "reason": "我要转人工"}, headers=customer
    )
    assert customer_transfer.status_code == 403
    assert _error_code(customer_transfer) == "TICKET_FORBIDDEN"

    anonymous_calls = [
        ("get", f"/api/tickets/{ticket_id}", None),
        ("get", "/api/tickets", None),
        ("get", "/api/order/list", None),
        ("post", "/api/tickets/confirm", {"ticket_id": ticket_id}),
        ("post", "/api/tickets/transfer", {"ticket_id": ticket_id}),
    ]
    for method, path, payload in anonymous_calls:
        response = getattr(flow, method)(path, json=payload) if payload else getattr(flow, method)(path)
        assert response.status_code == 401, (path, response.text)
        assert _error_code(response) == "TICKET_UNAUTHORIZED"

    # 普通用户的列表与统计只看自己：管理员那张单不会出现在他的视图里
    listing = flow.get("/api/tickets", headers=newbie).json()
    assert listing["total"] == 0
    assert flow.get("/api/tickets/stats", headers=newbie).json()["stats"]["total"] == 0


def test_登出后同一张令牌贯穿链路立即失效(flow):
    """登出作废令牌后，订单与工单接口同时回到未登录，不需要各接口单独处理。"""
    customer = _login(flow, CUSTOMER_USERNAME)
    assert flow.get("/api/order/list", headers=customer).status_code == 200

    logout = flow.post("/api/logout", headers=customer)
    assert logout.status_code == 200, logout.text
    assert logout.json()["revoked_access"] is True

    assert flow.get("/api/me", headers=customer).json()["user"] is None

    for path in ("/api/order/list", "/api/order/feature-scope?feature=批量导出", "/api/tickets"):
        response = flow.get(path, headers=customer)
        assert response.status_code == 401, (path, response.text)
        assert _error_code(response) == "TICKET_UNAUTHORIZED"


def test_凭证错误在链路入口被拦下(flow):
    """密码错误拿不到令牌，伪造令牌进不了链路，两者都不会漏到业务逻辑里。"""
    wrong = flow.post("/api/login", json={"username": CUSTOMER_USERNAME, "password": "WrongPass1"})
    assert wrong.status_code == 200
    assert wrong.json()["token"] == ""
    assert wrong.json()["user"] == {}

    forged = {"Authorization": "Bearer not-a-real-token"}
    assert flow.get("/api/me", headers=forged).json()["user"] is None
    assert flow.get("/api/order/list", headers=forged).status_code == 401
    assert _error_code(flow.get("/api/tickets", headers=forged)) == "TICKET_UNAUTHORIZED"
