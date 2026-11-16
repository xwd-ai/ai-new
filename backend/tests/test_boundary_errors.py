"""三模块边界与异常测试：入参撞到边界时必须是明确拒绝，而不是 500。

测试的判定标准只有两条：
1. 边界入参（空值、空串、超长、负数、非整数、类型不对、不存在的标识）
   要么被结构校验挡住（框架 422 + detail），要么被业务校验挡住
   （业务错误码 + 稳定状态码），任何一种都不能冒到服务端 500；
2. 拒绝时要给出可定位的线索：业务错误带 error_code，超长/越界类错误
   带 error_detail 指明是哪个字段，调用方改一个入参就能重试成功。

覆盖范围按模块划段：工单号与写操作入参、待确认超时阈值、批量收敛上限、
列表分页/状态筛选/关键词/排序、统计区间、认证注册与登录、订单查询与
管理侧筛选。全部用例使用独立临时 SQLite，不读写真实的 app.db。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api import tickets as api_tickets
from backend.api.auth import auth_router
from backend.database import Base
from backend.models import Order, Product, Session, User
from backend.models.ticket import TicketErrorCode
from backend.services import auth as auth_service
from backend.services import orders as orders_service
from backend.services import tickets as ticket_service

ADMIN_TOKEN = "tk-edge-admin"
CUSTOMER_TOKEN = "tk-edge-cust"
EMPTY_TOKEN = "tk-edge-empty"

ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
CUSTOMER = {"Authorization": f"Bearer {CUSTOMER_TOKEN}"}
EMPTY = {"Authorization": f"Bearer {EMPTY_TOKEN}"}

ADMIN_ID = "u-edge-admin"
CUSTOMER_ID = "u-edge-cust"
EMPTY_ID = "u-edge-empty"

CUSTOMER_PASSWORD = "Probe1234"


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    """固定签名密钥，避免边界用例受本机环境变量影响。"""
    monkeypatch.setenv(auth_service.JWT_SECRET_ENV, "unit-test-boundary-secret-0123456789")
    monkeypatch.delenv(auth_service.JWT_LEEWAY_ENV, raising=False)


def _seed(session_factory):
    """两个有订单的账号、一个没有订单的账号，各配一条会话。"""
    now = time.time()
    admin = User(id=ADMIN_ID, username="edge-admin", name="客服小张", role="admin")
    admin.set_password(CUSTOMER_PASSWORD)
    customer = User(id=CUSTOMER_ID, username="edge-cust", name="客户小李", role="customer")
    customer.set_password(CUSTOMER_PASSWORD)
    empty = User(id=EMPTY_ID, username="edge-empty", name="客户小赵", role="customer")
    empty.set_password(CUSTOMER_PASSWORD)

    db = session_factory()
    try:
        db.add_all(
            [
                admin,
                customer,
                empty,
                Session(token=ADMIN_TOKEN, user_id=ADMIN_ID, username="edge-admin",
                        name="客服小张", role="admin", expires_at=now + 3600),
                Session(token=CUSTOMER_TOKEN, user_id=CUSTOMER_ID, username="edge-cust",
                        name="客户小李", role="customer", expires_at=now + 3600),
                Session(token=EMPTY_TOKEN, user_id=EMPTY_ID, username="edge-empty",
                        name="客户小赵", role="customer", expires_at=now + 3600),
                Product(id="pro", name="专业版", features=["导出", "批量导出"]),
                Order(id="O-EDGE-1", user_id=CUSTOMER_ID, product_tier="pro", status="active",
                      started_at=now - 86400, expires_at=now + 86400 * 30),
            ]
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 三个账号；服务层的 SessionLocal 一并指向它。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'boundary.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (api_tickets, ticket_service, auth_service, orders_service):
        monkeypatch.setattr(module, "SessionLocal", session_factory)
    _seed(session_factory)
    yield session_factory
    engine.dispose()


@pytest.fixture()
def client(factory):
    """认证 + 工单（含订单）两组路由，即三模块对外的全部入口。"""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(api_tickets.tickets_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


def _error(response) -> tuple[int, str]:
    """取业务错误的 (状态码, 错误码)，顺带守住「错误体两个字段都在」。"""
    body = response.json()
    assert "error" in body and "error_code" in body, body
    assert body["error"], body
    return response.status_code, body["error_code"]


def _create(client, description: str, headers=CUSTOMER) -> str:
    """建单并返回工单号，草稿固定落在待确认状态。"""
    response = client.post("/api/tickets", json={"description": description}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["ticket"]["id"]


# ---------------------------------------------------------------------------
# 一、工单号入参：类型不对或查不到都按「不存在」处理，不能打到数据库绑定层
# ---------------------------------------------------------------------------


def test_工单号非字符串统一按不存在处理(factory):
    """对象、数组、数字、布尔、空值都不该走到 SQL 绑定，一律返回 None。"""
    for raw in ({"a": 1}, [1, 2], 12345, True, None, b"T-1"):
        assert ticket_service.get_ticket(raw) is None, raw

    # 合法但查不到的字符串同样返回 None，不抛异常
    assert ticket_service.get_ticket("T-999999") is None
    assert ticket_service.get_ticket("") is None


def test_工单号类型异常时接口返回不存在而不是服务器错误(client):
    """改状态、回复、确认、取消、详情五处取数入口共用同一道防线。"""
    cases = [
        ("post", "/api/tickets/status", {"ticket_id": {"a": 1}, "status": "resolved"}),
        ("post", "/api/tickets/status", {"ticket_id": [1], "status": "resolved"}),
        ("post", "/api/tickets/status", {"ticket_id": 12345, "status": "resolved"}),
        ("post", "/api/tickets/status", {"status": "resolved"}),
        ("post", "/api/tickets/reply", {"ticket_id": "  ", "content": "已收到"}),
        ("post", "/api/tickets/confirm", {"ticket_id": "T-999999"}),
        ("post", "/api/tickets/cancel", {"ticket_id": "T-999999", "reason": "不需要了"}),
    ]
    for method, url, payload in cases:
        response = getattr(client, method)(url, json=payload, headers=ADMIN)
        # 工单号为空白时由结构校验拦下（422），其余走到取数层得到 404
        assert response.status_code in (404, 422), (url, payload, response.text)
        body = response.json()
        if response.status_code == 404:
            assert body["error_code"] == TicketErrorCode.NOT_FOUND, body


def test_工单号超长与特殊字符路径同样按不存在处理(client):
    """路径参数不设上限，超长串与中文、表情符号都只应得到 404。"""
    for ticket_id in ("T-999999", "x" * 300, "工单不存在", "🚀"):
        response = client.get(f"/api/tickets/{ticket_id}", headers=ADMIN)
        assert response.status_code == 404, (ticket_id, response.text)
        assert response.json()["error_code"] == TicketErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# 二、待确认超时阈值与批量收敛上限：写错的参数当场拒绝，不静默改写
# ---------------------------------------------------------------------------


def test_待确认超时阈值拒绝负数与非整数(factory, client):
    """负阈值会把未到期草稿全判成超时，非整数说明入参类型写错，两者都必须拒绝。"""
    with pytest.raises(ticket_service.TicketValidationError) as negative:
        ticket_service.list_pending_tickets(timeout_seconds=-1)
    assert negative.value.code == TicketErrorCode.INVALID_PAYLOAD
    assert negative.value.details["field"] == "timeout_seconds"

    with pytest.raises(ticket_service.TicketValidationError):
        ticket_service.expire_pending_tickets(timeout_seconds="abc")

    # 0 是合法值：表示「立即视为超时」，不参与拒绝
    assert ticket_service.list_pending_tickets(timeout_seconds=0)["timeout_seconds"] == 0
    assert ticket_service.expire_pending_tickets(timeout_seconds=0)["closed"] == 0

    response = client.get("/api/tickets/pending?timeout_seconds=-5", headers=CUSTOMER)
    status, code = _error(response)
    assert (status, code) == (422, TicketErrorCode.INVALID_PAYLOAD)
    assert response.json()["error_detail"]["field"] == "timeout_seconds"

    response = client.post("/api/tickets/expire?timeout_seconds=-1", headers=ADMIN)
    assert _error(response) == (422, TicketErrorCode.INVALID_PAYLOAD)

    # 非整数由类型声明挡在框架层，同样是 422，只是错误体形态不同
    response = client.post("/api/tickets/expire?timeout_seconds=abc", headers=ADMIN)
    assert response.status_code == 422 and "detail" in response.json()


def test_批量收敛上限边界(factory, client):
    """1 与上限本身放行，0、超上限与非整数拒绝。"""
    assert ticket_service._expire_batch_size(1) == 1
    assert ticket_service._expire_batch_size(ticket_service.EXPIRE_BATCH_MAX) == (
        ticket_service.EXPIRE_BATCH_MAX
    )
    for bad in (0, -1, ticket_service.EXPIRE_BATCH_MAX + 1):
        with pytest.raises(ticket_service.TicketValidationError) as excinfo:
            ticket_service._expire_batch_size(bad)
        assert excinfo.value.code == TicketErrorCode.INVALID_BATCH

    for bad in (0, 501):
        response = client.post(f"/api/tickets/expire?limit={bad}", headers=ADMIN)
        status, code = _error(response)
        assert (status, code) == (422, TicketErrorCode.INVALID_BATCH), bad
        assert response.json()["error_detail"]["field"] == "limit"

    response = client.post("/api/tickets/expire?limit=500", headers=ADMIN)
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 三、列表查询入参：分页、状态筛选、关键词、排序各自守住边界
# ---------------------------------------------------------------------------


def test_分页入参边界(client):
    """页码从 1 起、每页上限 100；超范围页码给空列表而不是报错。"""
    _create(client, "分页边界用单一")
    _create(client, "分页边界用单二")

    for bad in (0, -1):
        response = client.get(f"/api/tickets?page={bad}", headers=CUSTOMER)
        assert _error(response) == (422, TicketErrorCode.INVALID_PAGINATION), bad

    for bad in (0, 101, 1000):
        response = client.get(f"/api/tickets?page_size={bad}", headers=CUSTOMER)
        assert _error(response) == (422, TicketErrorCode.INVALID_PAGINATION), bad

    # 上限本身放行
    response = client.get("/api/tickets?page=1&page_size=100", headers=CUSTOMER)
    assert response.status_code == 200, response.text

    # 超出末页：空列表 + 一致的 total/pages/has_more
    response = client.get("/api/tickets?page=99&page_size=20", headers=CUSTOMER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tickets"] == [] and body["page"] == 99
    assert body["has_more"] is False and body["pages"] >= 1
    assert body["total"] == 2


def test_状态筛选边界(client):
    """空值、空白、all 视为不筛选；大小写与重复项归一；未知状态明确拒绝。"""
    _create(client, "状态筛选边界用单")

    for raw in ("", "   ", "all", "ALL", ","):
        response = client.get(f"/api/tickets?status={raw}", headers=CUSTOMER)
        assert response.status_code == 200, (raw, response.text)
        assert response.json()["statuses"] == [], raw
        assert response.json()["total"] == 1, raw

    response = client.get("/api/tickets?status=PENDING_CONFIRM,pending_confirm", headers=CUSTOMER)
    assert response.status_code == 200, response.text
    assert response.json()["statuses"] == ["pending_confirm"]
    assert response.json()["total"] == 1

    response = client.get("/api/tickets?status=opened", headers=CUSTOMER)
    assert _error(response) == (422, TicketErrorCode.INVALID_STATUS_FILTER)


def test_关键词边界(client):
    """空白视为不搜索，50 字放行、51 字拒绝，通配符按字面量匹配。"""
    _create(client, "导出失败 100% 复现")
    _create(client, "登录后白屏")

    response = client.get("/api/tickets?keyword=%20%20%20", headers=CUSTOMER)
    assert response.status_code == 200, response.text
    assert response.json()["keyword"] is None and response.json()["total"] == 2

    exactly = "边" * ticket_service.MAX_KEYWORD_LENGTH
    response = client.get(f"/api/tickets?keyword={exactly}", headers=CUSTOMER)
    assert response.status_code == 200, response.text

    too_long = "边" * (ticket_service.MAX_KEYWORD_LENGTH + 1)
    response = client.get(f"/api/tickets?keyword={too_long}", headers=CUSTOMER)
    assert _error(response) == (422, TicketErrorCode.INVALID_KEYWORD)

    # % 与 _ 是字面量：只命中真的含这个字符的那条，不会把全量捞回来
    response = client.get("/api/tickets?keyword=%25", headers=CUSTOMER)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    response = client.get("/api/tickets?keyword=_", headers=CUSTOMER)
    assert response.status_code == 200 and response.json()["total"] == 0


def test_排序边界(client):
    """asc/desc 及书面写法放行，空值取默认逆序，其它值明确拒绝。"""
    _create(client, "排序边界用单")

    for raw, expected in (
        ("asc", "asc"),
        ("ASC", "asc"),
        ("ascending", "asc"),
        ("desc", "desc"),
        ("descending", "desc"),
    ):
        response = client.get(f"/api/tickets?order={raw}", headers=CUSTOMER)
        assert response.status_code == 200, (raw, response.text)
        assert response.json()["order"] == expected

    response = client.get("/api/tickets", headers=CUSTOMER)
    assert response.json()["order"] == "desc"

    response = client.get("/api/tickets?order=sideways", headers=CUSTOMER)
    assert _error(response) == (422, TicketErrorCode.INVALID_ORDER)


# ---------------------------------------------------------------------------
# 四、统计区间：1–365 天，越界与非整数都拒绝
# ---------------------------------------------------------------------------


def test_统计区间边界(client):
    """区间口径只在 1–365 之间成立，越界不静默截断。"""
    for bad in (0, -1, 366, 99999):
        response = client.get(f"/api/tickets/stats?days={bad}", headers=CUSTOMER)
        status, code = _error(response)
        assert (status, code) == (422, TicketErrorCode.INVALID_STATS_RANGE), bad
        detail = response.json()["error_detail"]
        assert detail["min_days"] == 1 and detail["max_days"] == 365
        assert detail["requested_days"] == bad

    for good in (1, 365):
        response = client.get(f"/api/tickets/stats?days={good}", headers=CUSTOMER)
        assert response.status_code == 200, (good, response.text)
        assert response.json()["stats"]["range"]["days"] == good

    response = client.get("/api/tickets/stats?days=abc", headers=CUSTOMER)
    assert response.status_code == 422 and "detail" in response.json()


# ---------------------------------------------------------------------------
# 五、写操作请求体：空白、超长、空列表一律拒绝，合法边界值放行
# ---------------------------------------------------------------------------


def test_建单请求体边界(client):
    """描述必填且非空白、上限 500 字；步骤与依据上限 1000 字。"""
    for payload in (
        {},
        {"description": ""},
        {"description": "    "},
        {"description": "\n\t "},
        {"description": "问" * 501},
        {"description": "导出失败", "tried_steps": "步" * 1001},
        {"description": "导出失败", "evidence": "据" * 1001},
    ):
        response = client.post("/api/tickets", json=payload, headers=CUSTOMER)
        assert response.status_code == 422, (payload, response.text)
        assert "detail" in response.json()

    # 上限本身放行：长度上限按原文计，首尾空白在通过校验后才裁掉
    response = client.post(
        "/api/tickets", json={"description": "问" * 500}, headers=CUSTOMER
    )
    assert response.status_code == 200, response.text
    assert response.json()["ticket"]["description"] == "问" * 500

    response = client.post(
        "/api/tickets", json={"description": "   需要排查的导出问题   "}, headers=CUSTOMER
    )
    assert response.status_code == 200, response.text
    assert response.json()["ticket"]["description"] == "需要排查的导出问题"


def test_批量工单号边界(client):
    """空列表、纯空白项、超过 50 个都拒绝；重复工单号只处理一次。"""
    for payload in (
        {"ticket_ids": []},
        {"ticket_ids": ["   "]},
        {"ticket_ids": ["", "\t"]},
        {"ticket_ids": [f"T-{index}" for index in range(51)]},
    ):
        response = client.post("/api/tickets/batch-close", json=payload, headers=ADMIN)
        assert response.status_code == 422, (payload, response.text)

    response = client.post(
        "/api/tickets/batch-close",
        json={"ticket_ids": ["T-999999", "T-999999", "T-999999"]},
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["failed"] == 1 and result["succeeded"] == 0


def test_回复内容边界(client):
    """回复正文空白或超过 2000 字拒绝。"""
    ticket_id = _create(client, "回复边界用单")
    # 草稿只能确认后回复，草稿状态本身由状态机拒绝人工回复
    assert client.post("/api/tickets/confirm", json={"ticket_id": ticket_id},
                       headers=CUSTOMER).status_code == 200
    for content in ("", "   ", "回" * 2001):
        response = client.post(
            "/api/tickets/reply",
            json={"ticket_id": ticket_id, "content": content},
            headers=ADMIN,
        )
        assert response.status_code == 422, (len(content), response.text)

    response = client.post(
        "/api/tickets/reply",
        json={"ticket_id": ticket_id, "content": "回" * 2000},
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 六、认证入参：用户名格式、密码强度、登录空值与超长密码
# ---------------------------------------------------------------------------


def test_注册用户名边界(client):
    """3–32 位、字母开头；边界放行，格式不符一律 422 并给出明确错误码。"""
    for username in ("ab", "1abc", "_abc", "abc def", "用户名abc", "a" * 33, "-abc"):
        response = client.post(
            "/api/register", json={"username": username, "password": "Good1234"}
        )
        assert response.status_code == 422, (username, response.text)
        assert response.json()["error_code"] == "AUTH_INVALID_USERNAME"

    # 3 位与 32 位是合法边界，首尾空白先裁掉再判断
    for username in ("abc", "   a" + "b" * 31 + "   "):
        response = client.post(
            "/api/register", json={"username": username, "password": "Good1234"}
        )
        assert response.status_code == 200, (username, response.text)
        assert response.json()["user"]["role"] == "customer"


def test_注册密码边界(client):
    """强度规则的每条边界都要挡住，并在 violations 里给出原因。"""
    cases = (
        "Ab12345",      # 7 位，长度不足
        "abcdefghij",   # 无数字
        "1234567890",   # 无字母
        "Abcd 1234",    # 含空白
        "A1" + "x" * 72,  # 超过 bcrypt 的 72 字节窗口
        "12345678",     # 常见弱口令
        "edgepass1",    # 含用户名（账号直接用这段）
        "aaaaaaaa1",    # 非同一字符但有其它规则问题时不至于误报，此例应通过
    )
    for password in cases:
        response = client.post(
            "/api/register", json={"username": "edgepass1", "password": password}
        )
        if password == "aaaaaaaa1":
            assert response.status_code == 200, (password, response.text)
            continue
        assert response.status_code == 422, (password, response.text)
        body = response.json()
        assert body["error_code"] == "AUTH_WEAK_PASSWORD"
        assert body["error_detail"]["violations"], body


def test_登录空值与超长密码不报服务器错误(client):
    """登录失败一律 200 + 空 token，不因为密码离谱而返回 500。"""
    for payload in (
        {"username": "", "password": ""},
        {"username": "edge-cust", "password": ""},
        {"username": "edge-cust", "password": "x" * 5000},
        {"username": "unknown-user", "password": CUSTOMER_PASSWORD},
        {"username": "edge-cust", "password": "   "},
    ):
        response = client.post("/api/login", json=payload)
        assert response.status_code == 200, (payload, response.text)
        body = response.json()
        assert body["token"] == "" and body["user"] == {}, payload

    # 缺字段属于结构问题，交给框架 422
    response = client.post("/api/login", json={"username": "edge-cust"})
    assert response.status_code == 422

    # 正常凭证仍可登录，说明上面的拒绝没有误伤合法路径
    response = client.post(
        "/api/login", json={"username": "edge-cust", "password": CUSTOMER_PASSWORD}
    )
    assert response.status_code == 200 and response.json()["token"]


def test_换发与登出缺令牌(client):
    """/refresh 与 /logout 对空凭证给 401 + 稳定错误码，乱码按格式错误拒绝。"""
    response = client.post("/api/refresh", json={})
    assert _error(response) == (401, "AUTH_TOKEN_MISSING")

    response = client.post("/api/logout")
    assert _error(response) == (401, "AUTH_TOKEN_MISSING")

    response = client.post("/api/refresh", json={"refresh_token": "not-a-token"})
    assert _error(response) == (401, "AUTH_TOKEN_MALFORMED")

    response = client.post("/api/refresh", json={"refresh_token": "a.b.c"})
    assert _error(response) == (401, "AUTH_TOKEN_MALFORMED")


# ---------------------------------------------------------------------------
# 七、订单边界：无订单账号、未知功能、管理侧筛选与详情
# ---------------------------------------------------------------------------


def test_无订单账号各接口给出空结论(client):
    """没有订单不是异常：/order 给空订单，其余接口给 has_order=false 的结论。"""
    response = client.get("/api/order", headers=EMPTY)
    assert response.status_code == 200, response.text
    order = response.json()["order"]
    assert order["has_order"] is False and order["message"]

    response = client.get("/api/order/list", headers=EMPTY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_order"] is False and body["order_count"] == 0 and body["orders"] == []

    response = client.get("/api/order/scope", headers=EMPTY)
    assert response.status_code == 200, response.text
    scope = response.json()
    assert scope["has_order"] is False
    assert scope["reason_code"] == "no_order"
    assert scope["allowed"] is False and scope["in_service"] is False

    response = client.get("/api/order/feature-scope?feature=批量导出", headers=EMPTY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_order"] is False and body["in_scope"] is False
    assert body["reason_code"] == "no_order" and body["covered_by"] is None
    assert body["feature"]["included"] is False

    response = client.get("/api/order/renewal", headers=EMPTY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_order"] is False and body["hints"] == [] and body["remind_count"] == 0


def test_订单功能入参边界(client):
    """功能名留空只判整体，未知功能给 feature_unknown，超长由类型声明挡住。"""
    response = client.get("/api/order/scope?feature=", headers=CUSTOMER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["feature"] is None
    assert body["in_service"] is True and body["allowed"] is True

    response = client.get("/api/order/scope?feature=量子计算", headers=CUSTOMER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reason_code"] == "feature_unknown"
    assert body["allowed"] is False and body["in_service"] is True

    response = client.get("/api/order/scope?feature=" + "功" * 51, headers=CUSTOMER)
    assert response.status_code == 422 and "detail" in response.json()

    # 他人订单号与实际不存在的订单号同样只给 no_order，不泄露归属
    for order_id in ("O-NOT-EXIST", "O-SOMEONE-ELSE"):
        response = client.get(f"/api/order/scope?order_id={order_id}", headers=CUSTOMER)
        assert response.status_code == 200, (order_id, response.text)
        assert response.json()["reason_code"] == "no_order"


def test_管理侧订单筛选边界(client):
    """页码越界由类型声明挡住，客户号与关键词超长走业务错误码。"""
    for bad in (0, -1):
        response = client.get(f"/api/order/admin?page={bad}", headers=ADMIN)
        assert response.status_code == 422 and "detail" in response.json(), bad

    for bad in (0, 101):
        response = client.get(f"/api/order/admin?page_size={bad}", headers=ADMIN)
        assert response.status_code == 422 and "detail" in response.json(), bad

    response = client.get("/api/order/admin?keyword=" + "x" * 65, headers=ADMIN)
    status, code = _error(response)
    assert (status, code) == (400, "ORDER_INVALID_FILTER")
    assert response.json()["error_detail"]["field"] == "keyword"

    response = client.get("/api/order/admin?user_id=" + "u" * 65, headers=ADMIN)
    assert _error(response) == (400, "ORDER_INVALID_FILTER")

    response = client.get("/api/order/admin?tier=旗舰版", headers=ADMIN)
    assert _error(response) == (400, "ORDER_INVALID_FILTER")

    response = client.get("/api/order/admin?status=paused", headers=ADMIN)
    assert _error(response) == (400, "ORDER_INVALID_FILTER")

    # 合法边界：空筛选、上限页长、超范围页码
    response = client.get("/api/order/admin?page_size=100", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["page_size"] == 100
    response = client.get("/api/order/admin?page=99", headers=ADMIN)
    assert response.status_code == 200 and response.json()["items"] == []
    response = client.get("/api/order/admin?status=all&tier=all&keyword=%20%20", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["statuses"] == [] and response.json()["tiers"] == []


def test_订单详情与状态流转边界(factory, client):
    """订单号查不到 404，未知状态与已退订终态的流转按各自错误码拒绝。"""
    response = client.get("/api/order/admin/O-NOT-EXIST", headers=ADMIN)
    status, code = _error(response)
    assert (status, code) == (404, "ORDER_NOT_FOUND")
    assert response.json()["error_detail"]["order_id"] == "O-NOT-EXIST"

    with pytest.raises(orders_service.OrderNotFoundError):
        orders_service.change_order_status("O-NOT-EXIST", "expired")

    with pytest.raises(orders_service.OrderNotFoundError):
        orders_service.change_order_status("", "expired")

    with pytest.raises(orders_service.OrderStatusTransitionError) as unknown:
        orders_service.change_order_status("O-EDGE-1", "paused")
    assert unknown.value.code == "ORDER_UNKNOWN_STATUS"

    # 同状态重复置位按幂等处理，不写库也不报错
    unchanged = orders_service.change_order_status("O-EDGE-1", "active")
    assert unchanged["unchanged"] is True and unchanged["to_status"] == "active"

    # 退订是终态：复活旧订单必须被状态机拒绝，且库里状态不变
    with factory() as db:
        db.add(Order(id="O-EDGE-CXL", user_id=ADMIN_ID, product_tier="pro",
                     status="cancelled", started_at=time.time() - 86400,
                     expires_at=time.time() + 86400))
        db.commit()

    with pytest.raises(orders_service.OrderStatusTransitionError) as terminal:
        orders_service.change_order_status("O-EDGE-CXL", "active")
    assert terminal.value.code == "ORDER_INVALID_TRANSITION"

    with factory() as db:
        assert db.query(Order).filter(Order.id == "O-EDGE-1").one().status == "active"
        assert db.query(Order).filter(Order.id == "O-EDGE-CXL").one().status == "cancelled"


# ---------------------------------------------------------------------------
# 八、整体兜底：任意边界入参都不得冒成服务器错误
# ---------------------------------------------------------------------------


def test_边界请求不产生服务器错误(client):
    """把最容易被写错的一批请求打一遍，只允许 2xx/4xx，不允许 5xx。"""
    requests = [
        ("get", "/api/tickets?page=&page_size=&order=&status=&keyword=", None, CUSTOMER),
        ("get", "/api/tickets?page=1&page_size=1&keyword=%25%5F", None, CUSTOMER),
        ("get", "/api/tickets/stats?days=1", None, CUSTOMER),
        ("get", "/api/tickets/pending?timeout_seconds=0&include_expired=true", None, CUSTOMER),
        ("get", "/api/tickets/transfer-decision?user_text=&feature=", None, CUSTOMER),
        ("get", "/api/order", None, EMPTY),
        ("get", "/api/order/renewal?window_days=0", None, CUSTOMER),
        ("get", "/api/order/renewal?window_days=365", None, CUSTOMER),
        ("get", "/api/me", None, {}),
        ("post", "/api/tickets", {"description": "x"}, {}),
        ("post", "/api/tickets/status", {}, ADMIN),
        ("post", "/api/tickets/status", {"ticket_id": None, "status": None}, ADMIN),
        ("post", "/api/tickets/transfer", {"ticket_id": "T-999999", "auto": True}, ADMIN),
        ("post", "/api/refresh", {"refresh_token": ""}, {}),
        ("post", "/api/logout", {}, {}),
    ]
    for method, url, payload, headers in requests:
        kwargs = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        response = getattr(client, method)(url, **kwargs)
        assert response.status_code < 500, (url, payload, response.status_code, response.text)

    # window_days 越界由类型声明挡住
    response = client.get("/api/order/renewal?window_days=366", headers=CUSTOMER)
    assert response.status_code == 422 and "detail" in response.json()
    response = client.get("/api/order/renewal?window_days=-1", headers=CUSTOMER)
    assert response.status_code == 422 and "detail" in response.json()
