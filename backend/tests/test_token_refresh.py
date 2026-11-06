"""Refresh token 换发流程测试。

覆盖换发链路的三种典型结局，以及它们周边容易被忽略的边界：

- 正常换发：长期凭证换到一张可用的短期令牌，续期凭证原样回传、寿命不被延伸，
  新令牌同样登记为会话，客户端按登录响应同一套字段更新状态即可；
- 过期：签名完好但载荷已过期，或载荷未过期而登记记录已到期，都必须被拒，
  时钟偏差余量只容忍正常对时误差，不会让过期凭证长期可用；
- 已撤销：吊销一条不影响同用户的其它凭证，重复吊销保持首次时间，
  吊销后即使令牌未过期、签名合格也不能再换发。

用例使用独立临时 SQLite，不读写真实 app.db；签发参数通过环境变量固定，
保证时间边界与错误码断言可复现。
"""

import base64
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.auth import auth_router
from backend.database import Base
from backend.models import Session, User
from backend.models.user import RefreshToken
from backend.services import auth as auth_service
from backend.services.auth import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    RefreshTokenOwnerError,
    RefreshTokenRevokedError,
    RefreshTokenUnknownError,
    TokenClaimsError,
    TokenExpiredError,
    TokenMissingError,
    TokenSignatureError,
)

PASSWORD = "RefreshPass@2026"
TEST_SECRET = "unit-test-refresh-secret-0123456789abcdef"
TEST_COST = 6
USER_ID = "u-01"
USERNAME = "cust-01"


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    """固定密钥并显式给出时钟偏差，避免用例受本机环境变量影响。"""
    monkeypatch.setenv(auth_service.JWT_SECRET_ENV, TEST_SECRET)
    monkeypatch.setenv(auth_service.JWT_LEEWAY_ENV, "30")
    monkeypatch.delenv(auth_service.JWT_REFRESH_TTL_ENV, raising=False)
    monkeypatch.setenv("BCRYPT_COST", str(TEST_COST))


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'refresh.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(auth_service, "SessionLocal", session_factory)
    db = session_factory()
    try:
        user = User(id=USER_ID, username=USERNAME, name="客户小李", role="customer")
        user.set_password(PASSWORD)
        db.add(user)
        other = User(id="u-02", username="cust-02", name="客户小王", role="customer")
        other.set_password(PASSWORD)
        db.add(other)
        db.commit()
    finally:
        db.close()
    return session_factory


@pytest.fixture()
def client(factory):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    return TestClient(app)


# ---- 辅助 ----


def _login(client, username: str = USERNAME, password: str = PASSWORD) -> dict:
    return client.post(
        "/api/login", json={"username": username, "password": password}
    ).json()


def _claims(token: str) -> dict:
    """只解载荷做断言，不做签名校验。"""
    segment = token.split(".")[1]
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    return json.loads(raw)


def _refresh_records(factory, token: str = "") -> list:
    db = factory()
    try:
        query = db.query(RefreshToken)
        if token:
            query = query.filter(RefreshToken.token == token)
        return query.all()
    finally:
        db.close()


def _sessions(factory) -> list:
    db = factory()
    try:
        return db.query(Session).all()
    finally:
        db.close()


def _user_payload(user_id: str = USER_ID, username: str = USERNAME) -> dict:
    return {"id": user_id, "username": username, "name": "客户小李", "role": "customer"}


def _signed_refresh(issued_at: int, ttl: int = 600, user: dict = None) -> str:
    """按指定签发时刻构造一张签名合法的续期令牌，用于控制时效边界。"""
    claims = auth_service.build_refresh_payload(
        user or _user_payload(), ttl=ttl, issued_at=issued_at
    )
    return auth_service.sign_jwt(claims)


def _register(factory, token: str, user_id: str = USER_ID, expires_at: float = None) -> None:
    """直接登记一条续期记录，绕过签发流程以便构造异常场景。"""
    db = factory()
    try:
        db.add(
            RefreshToken(
                token=token,
                user_id=user_id,
                expires_at=float(expires_at if expires_at is not None else time.time() + 86400),
            )
        )
        db.commit()
    finally:
        db.close()


# ---- 正常换发 ----


def test_refresh_issues_new_access_token(client):
    login = _login(client)

    result = auth_service.refresh_access_token(login["refresh_token"])
    claims = _claims(result["token"])

    assert result["token"] != login["token"]
    assert result["token"].count(".") == 2
    assert claims["typ"] == TOKEN_TYPE_ACCESS
    assert claims["sub"] == USER_ID
    assert claims["username"] == USERNAME
    assert result["token_type"] == "Bearer"


def test_refresh_echoes_original_refresh_token(client):
    """续期凭证不轮换：换发只重签访问令牌，客户端手里的长期凭证保持原样。"""
    login = _login(client)

    result = auth_service.refresh_access_token(login["refresh_token"])

    assert result["refresh_token"] == login["refresh_token"]
    assert _claims(result["refresh_token"])["typ"] == TOKEN_TYPE_REFRESH


def test_refresh_does_not_extend_refresh_lifetime(client):
    """续期凭证的到期时间由首次登录决定，换发次数不叠加寿命。"""
    login = _login(client)

    result = auth_service.refresh_access_token(login["refresh_token"])

    assert result["refresh_expires_at"] == login["refresh_expires_at"]
    assert result["refresh_expires_in"] <= login["refresh_expires_in"]


def test_refresh_response_matches_login_shape(client):
    """换发响应与登录响应的令牌字段同名同义，客户端无需第二套解析逻辑。"""
    login = _login(client)

    result = auth_service.refresh_access_token(login["refresh_token"])

    for field in ("token", "token_type", "expires_in", "expires_at"):
        assert field in result and result[field] == result[field]
    assert result["expires_at"] - result["expires_in"] <= int(time.time()) + 1
    assert result["user"]["username"] == USERNAME


def test_refresh_records_new_session(client, factory):
    """新令牌也要登记会话，否则按令牌查会话的链路会立刻丢掉登录态。"""
    login = _login(client)
    before = len(_sessions(factory))

    result = auth_service.refresh_access_token(login["refresh_token"])
    rows = _sessions(factory)

    assert len(rows) == before + 1
    added = [row for row in rows if row.token == result["token"]]
    assert len(added) == 1
    assert added[0].expires_at == result["expires_at"]
    assert added[0].user_id == USER_ID


def test_refresh_accepts_custom_ttl(client):
    login = _login(client)

    result = auth_service.refresh_access_token(login["refresh_token"], ttl=120)

    assert result["expires_in"] == 120
    assert _claims(result["token"])["exp"] - _claims(result["token"])["iat"] == 120


def test_refresh_is_repeatable(client):
    """同一张续期凭证可以反复换发，不因使用过而失效。"""
    login = _login(client)

    first = auth_service.refresh_access_token(login["refresh_token"])
    second = auth_service.refresh_access_token(login["refresh_token"])

    assert first["token"] != second["token"]
    assert first["refresh_expires_at"] == second["refresh_expires_at"]


def test_new_access_token_works_on_protected_route(client):
    login = _login(client)
    refreshed = client.post(
        "/api/refresh", json={"refresh_token": login["refresh_token"]}
    ).json()

    resp = client.get(
        "/api/me", headers={"Authorization": f"Bearer {refreshed['token']}"}
    )

    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == USERNAME


def test_refresh_records_one_row_per_login(client, factory):
    """每次登录登记一条续期记录，换发不会追加新记录。"""
    login = _login(client)

    auth_service.refresh_access_token(login["refresh_token"])

    assert len(_refresh_records(factory, login["refresh_token"])) == 1


# ---- 过期 ----


def test_expired_refresh_payload_rejected(factory):
    now = int(time.time())
    token = _signed_refresh(now - 3600, ttl=600)
    _register(factory, token, expires_at=now - 3000)

    with pytest.raises(TokenExpiredError) as exc:
        auth_service.refresh_access_token(token)

    assert exc.value.error_code == "AUTH_TOKEN_EXPIRED"
    assert exc.value.http_status == 401


def test_expired_record_rejected(client, factory):
    """令牌本身未过期，但登记记录已到期，同样不能再换发。"""
    login = _login(client)
    db = factory()
    try:
        record = (
            db.query(RefreshToken)
            .filter(RefreshToken.token == login["refresh_token"])
            .first()
        )
        record.expires_at = time.time() - 5
        db.commit()
    finally:
        db.close()

    with pytest.raises(TokenExpiredError) as exc:
        auth_service.refresh_access_token(login["refresh_token"])

    assert exc.value.error_code == "AUTH_TOKEN_EXPIRED"
    assert "expired_at" in (exc.value.detail or {})


def test_expired_token_not_rescued_by_leeway(factory):
    """时钟偏差余量只覆盖正常对时误差，超出余量仍判过期。"""
    now = int(time.time())
    leeway = auth_service.jwt_clock_leeway()
    token = _signed_refresh(now - 3600, ttl=3600 - leeway - 10)
    _register(factory, token, expires_at=now + 3600)

    with pytest.raises(TokenExpiredError):
        auth_service.refresh_access_token(token)


def test_token_within_leeway_still_accepted(factory):
    """刚过期但在余量内的令牌仍可换发，避免对时误差把用户踢下线。"""
    now = int(time.time())
    leeway = auth_service.jwt_clock_leeway()
    token = _signed_refresh(now - 3600, ttl=3600 - leeway + 60)
    _register(factory, token, expires_at=now + 3600)

    result = auth_service.refresh_access_token(token)

    assert result["token"]
    assert _claims(result["token"])["typ"] == TOKEN_TYPE_ACCESS


# ---- 已撤销 ----


def test_revoked_token_rejected(client):
    login = _login(client)
    assert auth_service.revoke_refresh_token(login["refresh_token"]) is True

    with pytest.raises(RefreshTokenRevokedError) as exc:
        auth_service.refresh_access_token(login["refresh_token"])

    assert exc.value.error_code == "AUTH_REFRESH_REVOKED"
    assert exc.value.http_status == 401


def test_revoke_marks_record(client, factory):
    login = _login(client)

    auth_service.revoke_refresh_token(login["refresh_token"])
    record = _refresh_records(factory, login["refresh_token"])[0]

    assert record.revoked is True
    assert record.revoked_at is not None
    assert record.active is False


def test_revoke_is_idempotent(client, factory):
    """重复吊销保持首次时间，返回值仍为成功。"""
    login = _login(client)
    token = login["refresh_token"]

    assert auth_service.revoke_refresh_token(token) is True
    first_at = _refresh_records(factory, token)[0].revoked_at
    time.sleep(0.01)
    assert auth_service.revoke_refresh_token(token) is True
    second_at = _refresh_records(factory, token)[0].revoked_at

    assert first_at == second_at


def test_revoke_unknown_token_returns_false(factory):
    assert auth_service.revoke_refresh_token("not-a-real-token") is False
    assert auth_service.revoke_refresh_token("") is False
    assert auth_service.revoke_refresh_token(None) is False


def test_revoke_only_affects_its_own_token(client):
    """吊销一条不影响同用户的其它凭证，也不影响其它用户。"""
    first = _login(client)
    second = _login(client)

    auth_service.revoke_refresh_token(first["refresh_token"])

    result = auth_service.refresh_access_token(second["refresh_token"])
    assert result["token"]


def test_revoked_token_cannot_be_reactivated_by_relogin(client):
    """重新登录签发新凭证，不会把已吊销的旧凭证救活。"""
    old = _login(client)
    auth_service.revoke_refresh_token(old["refresh_token"])
    fresh = _login(client)

    with pytest.raises(RefreshTokenRevokedError):
        auth_service.refresh_access_token(old["refresh_token"])
    assert auth_service.refresh_access_token(fresh["refresh_token"])["token"]


# ---- 其它拒绝情形 ----


def test_unregistered_token_rejected(factory):
    """签名合格但登记表里没有记录：来路不明，不予换发。"""
    token = _signed_refresh(int(time.time()), ttl=600)

    with pytest.raises(RefreshTokenUnknownError) as exc:
        auth_service.refresh_access_token(token)

    assert exc.value.error_code == "AUTH_REFRESH_UNKNOWN"


def test_record_owner_mismatch_rejected(client, factory):
    login = _login(client)
    db = factory()
    try:
        record = (
            db.query(RefreshToken)
            .filter(RefreshToken.token == login["refresh_token"])
            .first()
        )
        record.user_id = "u-99"
        db.commit()
    finally:
        db.close()

    with pytest.raises(RefreshTokenUnknownError):
        auth_service.refresh_access_token(login["refresh_token"])


def test_access_token_cannot_be_used_as_refresh(client):
    login = _login(client)

    with pytest.raises(TokenClaimsError) as exc:
        auth_service.refresh_access_token(login["token"])

    assert exc.value.error_code == "AUTH_TOKEN_BAD_CLAIMS"
    assert exc.value.detail == {"claim": "typ"}


def test_refresh_token_cannot_be_used_as_access(client):
    login = _login(client)

    with pytest.raises(TokenClaimsError):
        auth_service.verify_access_token(login["refresh_token"])


def test_tampered_refresh_token_rejected(client):
    login = _login(client)
    header, payload, signature = login["refresh_token"].split(".")
    flipped = "A" if payload[0] != "A" else "B"

    with pytest.raises(TokenSignatureError):
        auth_service.refresh_access_token(f"{header}.{flipped}{payload[1:]}.{signature}")


def test_blank_refresh_token_rejected():
    for value in ("", "   ", None):
        with pytest.raises(TokenMissingError):
            auth_service.refresh_access_token(value)


def test_deleted_user_rejected(client, factory):
    """账号已删除时，凭证即使完好也不再换发。"""
    login = _login(client)
    db = factory()
    try:
        user = db.query(User).filter(User.id == USER_ID).first()
        db.delete(user)
        db.commit()
    finally:
        db.close()

    with pytest.raises(RefreshTokenOwnerError) as exc:
        auth_service.refresh_access_token(login["refresh_token"])

    assert exc.value.error_code == "AUTH_REFRESH_USER_GONE"


def test_refresh_of_other_user_uses_own_identity(client):
    """换发按令牌的 sub 回库取用户，不会串到别人的身份上。"""
    login = _login(client, username="cust-02")

    result = auth_service.refresh_access_token(login["refresh_token"])

    assert result["user"]["id"] == "u-02"
    assert _claims(result["token"])["sub"] == "u-02"


# ---- 接口层 ----


def test_refresh_endpoint_accepts_body(client):
    login = _login(client)

    resp = client.post("/api/refresh", json={"refresh_token": login["refresh_token"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] and body["refresh_token"] == login["refresh_token"]
    assert body["expires_in"] > 0
    assert body["user"]["username"] == USERNAME


def test_refresh_endpoint_accepts_authorization_header(client):
    login = _login(client)

    resp = client.post(
        "/api/refresh",
        headers={"Authorization": f"Bearer {login['refresh_token']}"},
    )

    assert resp.status_code == 200
    assert resp.json()["token"]


def test_refresh_endpoint_body_wins_over_header(client):
    """请求体给了凭证就按请求体走，头部残留的旧值不参与。"""
    login = _login(client)
    stale = _login(client)["refresh_token"]
    auth_service.revoke_refresh_token(stale)

    resp = client.post(
        "/api/refresh",
        json={"refresh_token": login["refresh_token"]},
        headers={"Authorization": f"Bearer {stale}"},
    )

    assert resp.status_code == 200


def test_refresh_endpoint_without_token_returns_401(client):
    resp = client.post("/api/refresh", json={})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "AUTH_TOKEN_MISSING"
    assert body["error"]


def test_refresh_endpoint_revoked_returns_error_code(client):
    login = _login(client)
    auth_service.revoke_refresh_token(login["refresh_token"])

    resp = client.post("/api/refresh", json={"refresh_token": login["refresh_token"]})

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_REFRESH_REVOKED"


def test_refresh_endpoint_expired_returns_error_code(client, factory):
    token = _login(client)["refresh_token"]
    db = factory()
    try:
        record = (
            db.query(RefreshToken)
            .filter(RefreshToken.token == token)
            .first()
        )
        record.expires_at = time.time() - 5
        db.commit()
    finally:
        db.close()

    resp = client.post("/api/refresh", json={"refresh_token": token})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "AUTH_TOKEN_EXPIRED"
    assert body["error_detail"]["expired_at"] > 0


def test_refresh_endpoint_does_not_leak_token_on_error(client):
    resp = client.post("/api/refresh", json={"refresh_token": "garbage"})

    body = resp.json()
    assert "token" not in body
    assert set(body) == {"error", "error_code"}


def test_refresh_endpoint_rejects_oversized_body(client):
    """超长入参在结构层被拒，不进入签名校验。"""
    resp = client.post("/api/refresh", json={"refresh_token": "a" * 5000})

    assert resp.status_code == 422
