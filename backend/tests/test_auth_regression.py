"""认证模块回归测试。

站在模块边界上跑完整链路，把对外行为当作契约钉住；单个能力的细节
（签发声明、签名篡改、密码强度、吊销名单）已有各自的用例覆盖，这里不重复，
只盯「改了某一块之后，整条链路是否还说得通」。

四组回归：

1. 生命周期：注册 → 登录 → 取身份 → 换发 → 登出 → 旧凭证失效。
   每一步同时断言响应字段与落库状态，任何一环退化都会在同一个用例里暴露；
2. 响应契约：成功分支的字段集合逐键比对（多出字段同样算破坏契约），
   凭证错误保持既有形态，错误响应固定为 {error, error_code, error_detail?}；
3. 令牌用途：访问令牌与续期令牌不能互相顶替，旧版单段令牌仍能解析出身份，
   过期会话在解析时被就地删除；
4. 口径一致：角色回库判定、清理只删过期记录、服务层与接口层给出同一结论。

用例使用独立临时 SQLite，不读写真实 app.db；签名密钥、有效期与 bcrypt
成本通过环境变量固定，保证错误码与时间断言可复现。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.auth import auth_router
from backend.database import Base
from backend.models import Session, User
from backend.models.user import RefreshToken, RevokedToken
from backend.services import auth as auth_service

TEST_SECRET = "unit-test-regression-secret-0123456789"
TEST_COST = 6

ADMIN_ID = "u-reg-admin"
ADMIN_NAME = "regadmin"
ADMIN_PASSWORD = "AdminPass2026"

CUSTOMER_ID = "u-reg-cust"
CUSTOMER_NAME = "regacct"
CUSTOMER_PASSWORD = "CustPass2026"

# 登录成功时应当出现的全部字段：少一个客户端拿不到有效期，多一个说明契约被改动
LOGIN_KEYS = {
    "token",
    "user",
    "token_type",
    "expires_in",
    "expires_at",
    "refresh_token",
    "refresh_expires_in",
    "refresh_expires_at",
}
PUBLIC_USER_KEYS = {"id", "username", "name", "role"}


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    """固定签发参数，避免用例受本机环境变量影响。"""
    monkeypatch.setenv(auth_service.JWT_SECRET_ENV, TEST_SECRET)
    monkeypatch.setenv(auth_service.JWT_LEEWAY_ENV, "30")
    monkeypatch.setenv("BCRYPT_COST", str(TEST_COST))
    for name in (
        auth_service.JWT_TTL_ENV,
        auth_service.JWT_REFRESH_TTL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 一个管理员、一个客户，均使用当前算法的密码哈希。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth-regression.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(auth_service, "SessionLocal", session_factory)

    db = session_factory()
    try:
        for user_id, username, name, role, password in (
            (ADMIN_ID, ADMIN_NAME, "客服小张", "admin", ADMIN_PASSWORD),
            (CUSTOMER_ID, CUSTOMER_NAME, "客户小李", "customer", CUSTOMER_PASSWORD),
        ):
            user = User(id=user_id, username=username, name=name, role=role)
            user.set_password(password)
            db.add(user)
        db.commit()
    finally:
        db.close()

    yield session_factory
    engine.dispose()


@pytest.fixture()
def client(factory):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _login(client, username: str = CUSTOMER_NAME, password: str = CUSTOMER_PASSWORD) -> dict:
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _columns(factory, model, column, **filters) -> list:
    """按条件取一列值，返回普通列表，避免把 ORM 对象带出会话。"""
    db = factory()
    try:
        query = db.query(column)
        for key, value in filters.items():
            query = query.filter(getattr(model, key) == value)
        return [row[0] for row in query]
    finally:
        db.close()


def _set_role(factory, user_id: str, role: str) -> None:
    db = factory()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        user.role = role
        db.commit()
    finally:
        db.close()


def _assert_error(response, status: int, code: str) -> dict:
    """错误响应只允许三个键，error 必有、error_code 必中、detail 可选。"""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) <= {"error", "error_code", "error_detail"}
    assert body["error_code"] == code
    assert isinstance(body["error"], str) and body["error"].strip()
    if "error_detail" in body:
        assert isinstance(body["error_detail"], dict)
    return body


# ---------------------------------------------------------------------------
# 一、生命周期
# ---------------------------------------------------------------------------


def test_auth_lifecycle_from_register_to_logout(client, factory):
    """注册到登出的全链路：每一步的响应与落库状态都对得上。"""
    # 1. 注册：公开入口固定建成客户账号，返回体不含密码字段
    registered = client.post(
        "/api/register",
        json={"username": "regprobe", "password": "ProbePass2026", "name": "回归探针"},
    )
    assert registered.status_code == 200, registered.text
    body = registered.json()
    assert set(body) == {"user", "message"}
    assert body["message"] == "注册成功"
    assert set(body["user"]) == PUBLIC_USER_KEYS
    assert body["user"]["role"] == auth_service.CUSTOMER_ROLE
    assert body["user"]["name"] == "回归探针"
    user_id = body["user"]["id"]

    # 2. 登录：两类令牌各司其职
    issued = _login(client, "regprobe", "ProbePass2026")
    access, refresh = issued["token"], issued["refresh_token"]
    assert access.count(".") == 2 and refresh.count(".") == 2
    assert issued["token_type"] == "Bearer"
    assert issued["expires_in"] == auth_service.JWT_ACCESS_TTL
    assert issued["refresh_expires_in"] == auth_service.JWT_REFRESH_TTL

    claims = auth_service.verify_access_token(access)
    assert claims["sub"] == user_id
    assert claims["typ"] == auth_service.TOKEN_TYPE_ACCESS
    assert claims["exp"] == issued["expires_at"]

    # 3. 取身份：字段与注册时一致，且不带密码哈希
    me = client.get("/api/me", headers=_headers(access)).json()
    assert set(me["user"]) == PUBLIC_USER_KEYS
    assert me["user"]["id"] == user_id

    # 4. 换发：新访问令牌可用，续期凭证原样回传、寿命不重置
    renewed_response = client.post("/api/refresh", json={"refresh_token": refresh})
    assert renewed_response.status_code == 200, renewed_response.text
    renewed = renewed_response.json()
    assert renewed["token"] != access
    assert renewed["refresh_token"] == refresh
    assert renewed["refresh_expires_at"] == issued["refresh_expires_at"]
    assert abs((renewed["expires_at"] - time.time()) - renewed["expires_in"]) <= 2
    assert client.get("/api/me", headers=_headers(renewed["token"])).json()["user"]["id"] == user_id

    # 换发只补发短期凭证，已签发的访问令牌不受影响（有效期本就短，无需逐张作废）
    assert client.get("/api/me", headers=_headers(access)).json()["user"]["id"] == user_id

    # 5. 登出：访问令牌入黑名单、会话删除、续期令牌一并吊销
    out = client.post(
        "/api/logout",
        json={"refresh_token": refresh},
        headers=_headers(renewed["token"]),
    )
    assert out.status_code == 200, out.text
    out_body = out.json()
    assert out_body["revoked_access"] is True
    assert out_body["revoked_refresh"] is True
    assert out_body["session_removed"] is True
    assert out_body["user"]["id"] == user_id
    assert out_body["refresh_expires_at"] == issued["refresh_expires_at"]

    # 6. 失效：同一张访问令牌按未登录处理，续期令牌不能再换发
    assert client.get("/api/me", headers=_headers(renewed["token"])).json() == {"user": None}
    again = client.post("/api/refresh", json={"refresh_token": refresh})
    _assert_error(again, 401, "AUTH_REFRESH_REVOKED")

    # 落库状态：被登出的那条会话已删除，换发不会连带注销先前签发的短期令牌，
    # 它留给自身的有效期收尾，因此会话表里只会剩下最初登录的那一条
    sessions_left = _columns(factory, Session, Session.token, user_id=user_id)
    assert renewed["token"] not in sessions_left
    assert sessions_left == [access]

    staled = _columns(factory, RefreshToken, RefreshToken.revoked_at, user_id=user_id)
    assert len(staled) == 1 and staled[0] is not None
    jtis = _columns(factory, RevokedToken, RevokedToken.jti, user_id=user_id)
    assert len(jtis) == 2
    types = set(_columns(factory, RevokedToken, RevokedToken.token_type, user_id=user_id))
    assert types == {auth_service.TOKEN_TYPE_ACCESS, auth_service.TOKEN_TYPE_REFRESH}


def test_relogin_after_logout_is_unaffected(client):
    """登出只作废当次凭证，重新登录仍能正常签发。"""
    first = _login(client)
    client.post("/api/logout", headers=_headers(first["token"]))

    second = _login(client)

    assert second["token"] != first["token"]
    assert client.get("/api/me", headers=_headers(second["token"])).json()["user"]["id"] == CUSTOMER_ID
    assert client.get("/api/me", headers=_headers(first["token"])).json() == {"user": None}


# ---------------------------------------------------------------------------
# 二、响应契约
# ---------------------------------------------------------------------------


def test_login_success_shape_is_frozen(client):
    body = _login(client)

    assert set(body) == LOGIN_KEYS
    assert set(body["user"]) == PUBLIC_USER_KEYS
    assert body["token_type"] == "Bearer"
    assert body["expires_at"] == auth_service.verify_access_token(body["token"])["exp"]


def test_login_failure_keeps_legacy_shape(client, factory):
    """凭证错误仍返回 200 + 空令牌，且不产生任何会话或续期登记。"""
    for username, password in (
        (CUSTOMER_NAME, "WrongPass2026"),
        ("nobody-here", CUSTOMER_PASSWORD),
        ("", ""),
    ):
        response = client.post("/api/login", json={"username": username, "password": password})
        assert response.status_code == 200
        assert response.json() == {"token": "", "user": {}}

    assert _columns(factory, Session, Session.token) == []
    assert _columns(factory, RefreshToken, RefreshToken.token) == []


def test_register_response_shape_and_role(client):
    """注册返回的字段与角色口径固定，密码明文与哈希都不出现在响应里。"""
    response = client.post(
        "/api/register",
        json={"username": "shapeprobe", "password": "ShapePass2026"},
    )
    body = response.json()

    assert set(body) == {"user", "message"}
    assert set(body["user"]) == PUBLIC_USER_KEYS
    assert body["user"]["role"] == auth_service.CUSTOMER_ROLE
    # 未填显示名时取用户名，避免前端拿到空名字
    assert body["user"]["name"] == "shapeprobe"
    assert "ShapePass2026" not in response.text
    assert "password_hash" not in response.text


def test_error_bodies_and_codes_are_stable(client):
    """一组固定的失败场景，错误码与响应结构都不应随手改动。"""
    tokens = _login(client)

    cases = [
        ("换发缺令牌", client.post("/api/refresh", json={}), 401, "AUTH_TOKEN_MISSING"),
        (
            "换发乱码",
            client.post("/api/refresh", json={"refresh_token": "garbage"}),
            401,
            "AUTH_TOKEN_MALFORMED",
        ),
        (
            "拿访问令牌换发",
            client.post("/api/refresh", json={"refresh_token": tokens["token"]}),
            401,
            "AUTH_TOKEN_BAD_CLAIMS",
        ),
        ("登出缺令牌", client.post("/api/logout"), 401, "AUTH_TOKEN_MISSING"),
        (
            "登出乱码",
            client.post("/api/logout", headers=_headers("garbage")),
            401,
            "AUTH_TOKEN_MALFORMED",
        ),
        (
            "注册重名",
            client.post(
                "/api/register",
                json={"username": CUSTOMER_NAME.upper(), "password": "AnotherPass2026"},
            ),
            409,
            "AUTH_USERNAME_TAKEN",
        ),
        (
            "用户名格式",
            client.post("/api/register", json={"username": "1nope", "password": "AnotherPass2026"}),
            422,
            "AUTH_INVALID_USERNAME",
        ),
        (
            "密码强度",
            client.post("/api/register", json={"username": "weakpwd", "password": "123"}),
            422,
            "AUTH_WEAK_PASSWORD",
        ),
        ("清理未登录", client.post("/api/sessions/cleanup", json={}), 401, "AUTH_TOKEN_MISSING"),
    ]

    for label, response, status, code in cases:
        body = _assert_error(response, status, code)
        assert body["error"], label

    # 密码强度不足时逐条列出未通过的规则，前端据此逐项提示
    weak = client.post("/api/register", json={"username": "weakpwd", "password": "123"})
    assert isinstance(weak.json()["error_detail"]["violations"], list)

    # 非管理员清理：403 且说明所需角色与当前角色，便于区分「去登录」与「没权限」
    denied = _assert_error(
        client.post("/api/sessions/cleanup", json={}, headers=_headers(tokens["token"])),
        403,
        "AUTH_FORBIDDEN",
    )
    assert denied["error_detail"]["required_roles"] == [auth_service.ADMIN_ROLE]
    assert denied["error_detail"]["role"] == auth_service.CUSTOMER_ROLE


# ---------------------------------------------------------------------------
# 三、令牌用途与旧凭证兼容
# ---------------------------------------------------------------------------


def test_tokens_are_not_interchangeable(client):
    """两类令牌不能互相顶替：用途写在 typ 里，校验层逐处核对。"""
    tokens = _login(client)

    # 续期令牌当访问令牌用：解析不出身份，管理接口按未登录拒绝
    assert client.get("/api/me", headers=_headers(tokens["refresh_token"])).json() == {"user": None}
    _assert_error(
        client.post("/api/sessions/cleanup", json={}, headers=_headers(tokens["refresh_token"])),
        401,
        "AUTH_TOKEN_MISSING",
    )

    # 访问令牌当续期凭证用：拒绝并指出是用途不符
    mismatch = _assert_error(
        client.post("/api/refresh", json={"refresh_token": tokens["token"]}),
        401,
        "AUTH_TOKEN_BAD_CLAIMS",
    )
    assert mismatch["error_detail"]["claim"] == "typ"

    # 原始访问令牌未受这次失败影响
    assert client.get("/api/me", headers=_headers(tokens["token"])).json()["user"]["id"] == CUSTOMER_ID


def test_legacy_session_token_still_resolves(client, factory):
    """旧版单段令牌按会话表解析；过期记录在解析时被就地删除。"""
    now = time.time()
    db = factory()
    try:
        db.add_all(
            [
                Session(
                    token="tk-legacy-live",
                    user_id=CUSTOMER_ID,
                    username=CUSTOMER_NAME,
                    name="客户小李",
                    role="customer",
                    expires_at=now + 600,
                ),
                Session(
                    token="tk-legacy-stale",
                    user_id=CUSTOMER_ID,
                    username=CUSTOMER_NAME,
                    name="客户小李",
                    role="customer",
                    expires_at=now - 1,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    live = client.get("/api/me", headers=_headers("tk-legacy-live"))
    assert live.json()["user"]["id"] == CUSTOMER_ID

    stale = client.get("/api/me", headers=_headers("tk-legacy-stale"))
    assert stale.json() == {"user": None}

    # 过期会话不会被反复查询命中，解析时顺手删掉
    assert _columns(factory, Session, Session.token, token="tk-legacy-stale") == []
    assert _columns(factory, Session, Session.token, token="tk-legacy-live") == ["tk-legacy-live"]


# ---------------------------------------------------------------------------
# 四、角色口径与清理口径
# ---------------------------------------------------------------------------


def test_role_is_resolved_from_database(client, factory):
    """权限按库里当前角色判定，令牌里的角色快照不参与；改库即时生效。"""
    tokens = _login(client)

    _assert_error(
        client.post("/api/sessions/cleanup", json={}, headers=_headers(tokens["token"])),
        403,
        "AUTH_FORBIDDEN",
    )

    _set_role(factory, CUSTOMER_ID, auth_service.ADMIN_ROLE)
    assert client.post(
        "/api/sessions/cleanup", json={}, headers=_headers(tokens["token"])
    ).status_code == 200

    _set_role(factory, CUSTOMER_ID, auth_service.CUSTOMER_ROLE)
    _assert_error(
        client.post("/api/sessions/cleanup", json={}, headers=_headers(tokens["token"])),
        403,
        "AUTH_FORBIDDEN",
    )


def test_cleanup_purges_only_expired_rows(client, factory):
    """三张表一起收敛：过期即删，未过期的一条不动。"""
    admin = _login(client, ADMIN_NAME, ADMIN_PASSWORD)
    now = time.time()

    db = factory()
    try:
        db.add_all(
            [
                Session(token="tk-old", user_id=CUSTOMER_ID, username=CUSTOMER_NAME,
                        name="客户小李", role="customer", expires_at=now - 60),
                Session(token="tk-live", user_id=CUSTOMER_ID, username=CUSTOMER_NAME,
                        name="客户小李", role="customer", expires_at=now + 600),
                RefreshToken(token="rf-old", user_id=CUSTOMER_ID, expires_at=now - 60),
                RefreshToken(token="rf-live", user_id=CUSTOMER_ID, expires_at=now + 600),
                RevokedToken(jti="jti-old", user_id=CUSTOMER_ID, token_type="access",
                             expires_at=now - 60, revoked_at=now - 120, reason="logout"),
                RevokedToken(jti="jti-live", user_id=CUSTOMER_ID, token_type="access",
                             expires_at=now + 600, revoked_at=now - 10, reason="logout"),
            ]
        )
        db.commit()
    finally:
        db.close()

    body = client.post(
        "/api/sessions/cleanup", json={}, headers=_headers(admin["token"])
    ).json()

    assert body["deleted"] == {"sessions": 1, "refresh_tokens": 1, "revoked_tokens": 1}
    assert body["total"] == 3
    assert body["retention"] == 0
    assert body["truncated"] is False
    assert body["cutoff"] <= int(time.time())

    # 未过期记录全部保留；管理员本次登录自己登记的会话也不受影响
    assert _columns(factory, Session, Session.token, token="tk-live") == ["tk-live"]
    assert _columns(factory, Session, Session.token, token=admin["token"]) == [admin["token"]]
    assert _columns(factory, RefreshToken, RefreshToken.token, token="rf-live") == ["rf-live"]
    assert _columns(factory, RevokedToken, RevokedToken.jti, jti="jti-live") == ["jti-live"]


def test_cleanup_retention_window_and_batch_limit(client, factory):
    """retention 留观察窗口；limit 触顶时 truncated 提示还有剩余。"""
    admin = _login(client, ADMIN_NAME, ADMIN_PASSWORD)
    now = time.time()

    db = factory()
    try:
        for index in range(3):
            db.add(
                Session(
                    token=f"tk-sweep-{index}",
                    user_id=CUSTOMER_ID,
                    username=CUSTOMER_NAME,
                    name="客户小李",
                    role="customer",
                    expires_at=now - 10,
                )
            )
        db.commit()
    finally:
        db.close()

    # 多留 60 秒：刚过期 10 秒的三条都还在窗口内，一条也不删
    kept = client.post(
        "/api/sessions/cleanup", json={"retention": 60}, headers=_headers(admin["token"])
    ).json()
    assert kept["deleted"]["sessions"] == 0
    assert kept["retention"] == 60
    assert len(_columns(factory, Session, Session.token)) == 4  # 三条过期 + 管理员会话

    # 单次只删一条并标记 truncated，反复调用可继续收敛
    first = client.post(
        "/api/sessions/cleanup", json={"limit": 1}, headers=_headers(admin["token"])
    ).json()
    assert first["deleted"]["sessions"] == 1
    assert first["truncated"] is True

    second = client.post(
        "/api/sessions/cleanup", json={"limit": 1}, headers=_headers(admin["token"])
    ).json()
    assert second["deleted"]["sessions"] == 1

    final = client.post(
        "/api/sessions/cleanup", json={}, headers=_headers(admin["token"])
    ).json()
    assert final["deleted"]["sessions"] == 1
    assert final["truncated"] is False
    assert _columns(factory, Session, Session.token) == [admin["token"]]


def test_service_layer_agrees_with_http_layer(client, factory):
    """同一份凭证在服务层与接口层解析出同一个身份，避免只改坏一边。"""
    tokens = _login(client)

    account = auth_service.authenticate(CUSTOMER_NAME, CUSTOMER_PASSWORD)
    assert account["id"] == CUSTOMER_ID
    assert auth_service.authenticate(CUSTOMER_NAME, "WrongPass2026") is None

    resolved = auth_service.resolve_authorization(f"Bearer {tokens['token']}")
    assert resolved["id"] == CUSTOMER_ID
    # 响应里的用户信息就是服务层挑出的公开字段，两边不会各说一套
    assert tokens["user"] == auth_service.public_user(resolved)

    for bad in ("", "Bearer ", "garbage", f"Bearer {tokens['refresh_token']}"):
        assert auth_service.resolve_authorization(bad) is None
    assert client.get("/api/me", headers=_headers("garbage")).json() == {"user": None}
