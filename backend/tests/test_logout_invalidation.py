"""登出与令牌失效测试。

覆盖登出链路的落库语义与失效判定，以及它们周边容易被忽略的边界：

- 正常登出：访问令牌进作废名单、会话记录同步删除，返回体说明本次作废了什么；
  带上续期令牌时两条凭证一起失效，缺省时长期凭证留到自然过期；
- 幂等：重复登出返回同样的成功语义，不新增作废记录、不覆盖首次作废时间；
- 失效判定：登出后的令牌签名与时效都合格，但解析身份必须得到空——
  黑名单命中即未登录，续期换发同样被拦下，且不影响同用户其它凭证；
- 拒绝路径：空令牌、伪造签名、垃圾令牌一律拒绝且不写任何作废记录；
  续期令牌不属于当前用户时整体拒绝，不留「访问令牌已作废、续期没吊销」的半截结果。

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
from backend.models.user import RefreshToken, RevokedToken
from backend.services import auth as auth_service
from backend.services.auth import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    TokenClaimsError,
    TokenError,
    TokenExpiredError,
    TokenMissingError,
    TokenSignatureError,
)

PASSWORD = "LogoutPass@2026"
TEST_SECRET = "unit-test-logout-secret-0123456789abcdef"
TEST_COST = 6
USER_ID = "u-01"
USERNAME = "cust-01"
OTHER_ID = "u-02"
OTHER_USERNAME = "cust-02"


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
        f"sqlite:///{tmp_path / 'logout.db'}",
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
        other = User(id=OTHER_ID, username=OTHER_USERNAME, name="客户小王", role="customer")
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


def _revoked_rows(factory, jti: str = "") -> list:
    db = factory()
    try:
        query = db.query(RevokedToken)
        if jti:
            query = query.filter(RevokedToken.jti == jti)
        return query.all()
    finally:
        db.close()


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


def _expired_access_token(user: dict = None) -> str:
    """构造一张签名合法但载荷已过期的访问令牌。"""
    payload = auth_service.build_access_payload(
        user or {"id": USER_ID, "username": USERNAME, "role": "customer"},
        ttl=60,
        issued_at=int(time.time()) - 3600,
    )
    return auth_service.sign_jwt(payload)


def _tamper(token: str) -> str:
    """翻转载荷末位，签名必然对不上。"""
    header, payload, signature = token.split(".")
    flipped = ("A" if payload[-1] != "A" else "B")
    return f"{header}.{payload[:-1]}{flipped}.{signature}"


# ---- 正常登出：落库语义 ----


def test_logout_revokes_access_token_and_drops_session(client, factory):
    """登出后访问令牌进作废名单，对应会话记录一并删除。"""
    login = _login(client)
    claims = _claims(login["token"])

    result = auth_service.logout(login["token"])

    rows = _revoked_rows(factory, claims["jti"])
    assert len(rows) == 1
    record = rows[0]
    assert record.user_id == USER_ID
    assert record.token_type == TOKEN_TYPE_ACCESS
    assert record.reason == auth_service.REVOKE_REASON_LOGOUT
    assert record.expires_at == float(claims["exp"])
    assert result["revoked_access"] is True
    assert result["session_removed"] is True
    assert all(row.token != login["token"] for row in _sessions(factory))


def test_logout_returns_identity_and_expiry(client):
    login = _login(client)
    claims = _claims(login["token"])

    result = auth_service.logout(login["token"])

    assert result["message"]
    assert result["user"]["id"] == USER_ID
    assert result["user"]["username"] == USERNAME
    assert result["access_expires_at"] == int(claims["exp"])
    assert result["revoked_refresh"] is False
    assert result["refresh_expires_at"] is None


def test_logout_with_refresh_revokes_both_credentials(client, factory):
    """带上续期令牌时两条凭证一起作废：黑名单各一行，登记记录标记吊销。"""
    login = _login(client)
    access_claims = _claims(login["token"])
    refresh_claims = _claims(login["refresh_token"])

    result = auth_service.logout(login["token"], login["refresh_token"])

    assert result["revoked_access"] is True
    assert result["revoked_refresh"] is True
    assert result["refresh_expires_at"] == int(refresh_claims["exp"])
    access_rows = _revoked_rows(factory, access_claims["jti"])
    refresh_rows = _revoked_rows(factory, refresh_claims["jti"])
    assert access_rows and access_rows[0].token_type == TOKEN_TYPE_ACCESS
    assert refresh_rows and refresh_rows[0].token_type == TOKEN_TYPE_REFRESH
    records = _refresh_records(factory, login["refresh_token"])
    assert records and records[0].revoked is True


def test_logout_without_session_still_revokes(client, factory):
    """令牌没登记过会话（如手工签发）也要能作废，只是没有会话可删。"""
    payload = auth_service.build_access_payload(
        {"id": USER_ID, "username": USERNAME, "role": "customer"}, ttl=300
    )
    token = auth_service.sign_jwt(payload)

    result = auth_service.logout(token)

    assert result["revoked_access"] is True
    assert result["session_removed"] is False
    assert len(_revoked_rows(factory, payload["jti"])) == 1


def test_logout_keeps_first_revocation_time(client, factory):
    """重复登出不新增记录，也不覆盖首次作废时间。"""
    login = _login(client)
    jti = _claims(login["token"])["jti"]

    auth_service.logout(login["token"])
    first_at = _revoked_rows(factory, jti)[0].revoked_at
    time.sleep(0.01)
    result = auth_service.logout(login["token"])

    assert result["revoked_access"] is False
    assert result["session_removed"] is False
    rows = _revoked_rows(factory, jti)
    assert len(rows) == 1
    assert rows[0].revoked_at == first_at


def test_logout_with_refresh_is_idempotent(client, factory):
    login = _login(client)
    refresh_jti = _claims(login["refresh_token"])["jti"]

    auth_service.logout(login["token"], login["refresh_token"])
    result = auth_service.logout(login["token"], login["refresh_token"])

    assert result["revoked_access"] is False
    assert result["revoked_refresh"] is False
    assert len(_revoked_rows(factory, refresh_jti)) == 1


# ---- 正常登出：过期令牌仍可退出 ----


def test_expired_access_token_can_logout(client, factory):
    """客户端拿过期访问令牌来登出是正常时序，不该被时效判定挡在门外。"""
    token = _expired_access_token()
    with pytest.raises(TokenExpiredError):
        auth_service.verify_access_token(token)

    result = auth_service.logout(token)

    jti = _claims(token)["jti"]
    assert result["revoked_access"] is True
    assert len(_revoked_rows(factory, jti)) == 1


def test_expired_access_token_logout_endpoint(client):
    token = _expired_access_token()

    resp = client.post(
        "/api/logout", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["revoked_access"] is True
    assert body["user"]["id"] == USER_ID


# ---- 拒绝路径：不留下半截作废 ----


def test_blank_access_token_rejected():
    for value in ("", "   ", None):
        with pytest.raises(TokenMissingError):
            auth_service.logout(value)


def test_tampered_token_rejected_and_not_blacklisted(client, factory):
    """伪造签名的令牌直接拒绝，作废名单不能被外人塞记录。"""
    token = _tamper(_login(client)["token"])

    with pytest.raises(TokenSignatureError):
        auth_service.logout(token)

    assert _revoked_rows(factory) == []


def test_garbage_token_rejected(client, factory):
    with pytest.raises(TokenError):
        auth_service.logout("not-a-jwt")

    assert _revoked_rows(factory) == []


def test_foreign_refresh_rejected_before_any_revocation(client, factory):
    """续期令牌不属于当前用户时整体拒绝，访问令牌保持原样可用。"""
    login = _login(client)
    foreign = _login(client, username=OTHER_USERNAME)["refresh_token"]

    with pytest.raises(TokenClaimsError) as exc:
        auth_service.logout(login["token"], foreign)

    assert exc.value.error_code == "AUTH_TOKEN_BAD_CLAIMS"
    assert _revoked_rows(factory) == []
    assert auth_service.resolve_token(login["token"])["id"] == USER_ID
    assert auth_service.refresh_access_token(foreign)["user"]["id"] == OTHER_ID


def test_garbage_refresh_rejected_before_any_revocation(client, factory):
    login = _login(client)

    with pytest.raises(TokenError):
        auth_service.logout(login["token"], "not-a-jwt")

    assert _revoked_rows(factory) == []
    assert auth_service.resolve_token(login["token"])["id"] == USER_ID


# ---- 失效判定 ----


def test_revoked_access_token_resolves_to_none(client):
    """黑名单命中即未登录：签名与时效都合格也不再代表任何人。"""
    login = _login(client)
    auth_service.logout(login["token"])

    assert auth_service.resolve_token(login["token"]) is None
    assert auth_service.is_token_revoked(_claims(login["token"])["jti"]) is True


def test_me_endpoint_returns_null_user_after_logout(client):
    login = _login(client)
    client.post(
        "/api/logout", headers={"Authorization": f"Bearer {login['token']}"}
    )

    resp = client.get(
        "/api/me", headers={"Authorization": f"Bearer {login['token']}"}
    )

    assert resp.status_code == 200
    assert resp.json()["user"] is None


def test_refresh_rejected_after_logout_with_refresh(client):
    login = _login(client)
    auth_service.logout(login["token"], login["refresh_token"])

    resp = client.post(
        "/api/refresh", json={"refresh_token": login["refresh_token"]}
    )

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_REFRESH_REVOKED"


def test_access_only_logout_keeps_refresh_usable(client):
    """只给访问令牌时长期凭证留到自然过期，仍可换发新的访问令牌。"""
    login = _login(client)
    auth_service.logout(login["token"])

    result = auth_service.refresh_access_token(login["refresh_token"])

    assert result["token"] != login["token"]
    assert auth_service.resolve_token(result["token"])["id"] == USER_ID


def test_access_only_logout_refresh_record_stays_active(client, factory):
    login = _login(client)

    auth_service.logout(login["token"])

    records = _refresh_records(factory, login["refresh_token"])
    assert records and records[0].revoked is False
    assert auth_service.is_token_revoked(_claims(login["refresh_token"])["jti"]) is False


def test_other_user_token_unaffected_by_logout(client):
    """登出只作废自己的凭证，同表其它用户的登录不受牵连。"""
    mine = _login(client)
    other = _login(client, username=OTHER_USERNAME)

    auth_service.logout(mine["token"], mine["refresh_token"])

    assert auth_service.resolve_token(other["token"])["id"] == OTHER_ID
    assert auth_service.refresh_access_token(other["refresh_token"])["user"]["id"] == OTHER_ID


def test_unknown_jti_not_revoked(factory):
    assert auth_service.is_token_revoked(None) is False
    assert auth_service.is_token_revoked("") is False
    assert auth_service.is_token_revoked("no-such-jti") is False


# ---- 接口层：POST /api/logout ----


def test_logout_endpoint_success_shape(client, factory):
    login = _login(client)
    claims = _claims(login["token"])

    resp = client.post(
        "/api/logout",
        headers={"Authorization": f"Bearer {login['token']}"},
        json={"refresh_token": login["refresh_token"]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "message",
        "user",
        "revoked_access",
        "access_expires_at",
        "session_removed",
        "revoked_refresh",
        "refresh_expires_at",
    }
    assert body["message"]
    assert body["user"]["id"] == USER_ID
    assert body["revoked_access"] is True
    assert body["revoked_refresh"] is True
    assert body["session_removed"] is True
    assert body["access_expires_at"] == int(claims["exp"])
    assert body["refresh_expires_at"] == int(_claims(login["refresh_token"])["exp"])


def test_logout_endpoint_requires_authorization(client):
    resp = client.post("/api/logout", json={})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "AUTH_TOKEN_MISSING"
    assert body["error"]


def test_logout_endpoint_bearer_without_token_rejected(client):
    resp = client.post(
        "/api/logout", headers={"Authorization": "Bearer"}
    )

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_TOKEN_MISSING"


def test_logout_endpoint_garbage_token_rejected(client):
    resp = client.post(
        "/api/logout", headers={"Authorization": "Bearer not-a-jwt"}
    )

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_TOKEN_MALFORMED"


def test_logout_endpoint_tampered_token_rejected(client):
    token = _tamper(_login(client)["token"])

    resp = client.post(
        "/api/logout", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_TOKEN_BAD_SIGNATURE"


def test_logout_endpoint_repeat_is_idempotent(client):
    login = _login(client)
    headers = {"Authorization": f"Bearer {login['token']}"}

    first = client.post("/api/logout", headers=headers).json()
    second = client.post("/api/logout", headers=headers).json()

    assert first["revoked_access"] is True
    assert second["revoked_access"] is False
    assert second["message"]


def test_logout_endpoint_foreign_refresh_rejected(client):
    login = _login(client)
    foreign = _login(client, username=OTHER_USERNAME)["refresh_token"]

    resp = client.post(
        "/api/logout",
        headers={"Authorization": f"Bearer {login['token']}"},
        json={"refresh_token": foreign},
    )

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "AUTH_TOKEN_BAD_CLAIMS"
    # 半截结果不留下：访问令牌仍可正常解析
    me = client.get(
        "/api/me", headers={"Authorization": f"Bearer {login['token']}"}
    )
    assert me.json()["user"]["id"] == USER_ID


def test_logout_endpoint_accepts_bare_token(client):
    """只给令牌原文也接受，便于手工联调。"""
    login = _login(client)

    resp = client.post(
        "/api/logout", headers={"Authorization": login["token"]}
    )

    assert resp.status_code == 200
    assert resp.json()["revoked_access"] is True


# ---- 作废记录的生命周期 ----


def test_revocation_record_keeps_until_token_expiry(client, factory):
    """未过期的作废记录不被清理误删，令牌剩余寿命内始终拦截。"""
    login = _login(client)
    auth_service.logout(login["token"])
    jti = _claims(login["token"])["jti"]

    result = auth_service.purge_expired_sessions()

    assert result["deleted"]["revoked_tokens"] == 0
    assert len(_revoked_rows(factory, jti)) == 1
    assert auth_service.resolve_token(login["token"]) is None


def test_purge_removes_only_expired_revocation_records(client, factory):
    """过期令牌的作废记录可以整批清掉：时效校验本就会拦下它们。"""
    login = _login(client)
    expired_token = _expired_access_token()
    auth_service.logout(expired_token)  # 过期但登记的作废记录也已过期
    auth_service.logout(login["token"])  # 未过期，记录应保留

    result = auth_service.purge_expired_sessions()

    assert result["deleted"]["revoked_tokens"] == 1
    kept = [row.jti for row in _revoked_rows(factory)]
    assert kept == [_claims(login["token"])["jti"]]


def test_purge_with_retention_keeps_recently_expired(client, factory):
    """retention>0 时刚过期的记录留一段观察窗口，不立即清掉。"""
    expired_token = _expired_access_token()
    auth_service.logout(expired_token)
    jti = _claims(expired_token)["jti"]

    result = auth_service.purge_expired_sessions(retention=3600)

    assert result["deleted"]["revoked_tokens"] == 0
    assert len(_revoked_rows(factory, jti)) == 1
