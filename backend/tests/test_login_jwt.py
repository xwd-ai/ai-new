"""登录签发 JWT 测试。

覆盖签发侧的三件事：
- 登录成功返回的是三段式签名令牌，声明与用户、有效期对得上；
- 令牌被登记为会话记录，到期时间与令牌的 exp 一致，既有解析路径仍可用；
- 凭证错误的响应体与旧契约完全一致，不多出任何字段。

测试使用独立临时 SQLite，不读写真实的 app.db。
"""

import base64
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api.auth import auth_router
from backend.database import Base
from backend.models import Session, User
from backend.services import auth as auth_service
from backend.services.auth import JWT_ACCESS_TTL

PASSWORD = "JwtPass@2026"
TEST_COST = 6


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jwt.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(auth_service, "SessionLocal", session_factory)
    monkeypatch.setenv("BCRYPT_COST", str(TEST_COST))
    return session_factory


@pytest.fixture()
def client(factory):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    db = factory()
    try:
        user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
        user.set_password(PASSWORD)
        db.add(user)
        db.commit()
    finally:
        db.close()
    return TestClient(app)


def _login(client, password: str = PASSWORD) -> dict:
    return client.post("/api/login", json={"username": "cust-01", "password": password}).json()


def _payload(token: str) -> dict:
    """取出令牌中间段并解成声明集合，仅用于断言内容，不做签名校验。"""
    segment = token.split(".")[1]
    raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    return json.loads(raw)


def _sessions(factory) -> list:
    db = factory()
    try:
        return db.query(Session).all()
    finally:
        db.close()


# ---- 签发结果 ----


def test_login_returns_signed_token(client):
    body = _login(client)

    assert body["token"].count(".") == 2
    assert len(body["token"].split(".")) == 3
    assert body["token_type"] == "Bearer"
    assert body["user"]["username"] == "cust-01"


def test_token_payload_carries_identity_and_ttl(client):
    body = _login(client)
    claims = _payload(body["token"])

    assert claims["sub"] == "u-01"
    assert claims["username"] == "cust-01"
    assert claims["role"] == "customer"
    assert claims["typ"] == "access"
    assert claims["exp"] - claims["iat"] == JWT_ACCESS_TTL
    assert body["expires_in"] == JWT_ACCESS_TTL
    assert body["expires_at"] == claims["exp"]


def test_token_prefix_is_not_random_hex(client):
    """令牌是签名串而不是随机串：同样声明重新签发也会得到同样的头部段。"""
    first = _login(client)["token"].split(".")[0]
    second = _login(client)["token"].split(".")[0]

    assert first == second
    assert len(first) > 16


# ---- 会话登记 ----


def test_token_is_recorded_as_session(client, factory):
    body = _login(client)
    rows = _sessions(factory)

    assert len(rows) == 1
    assert rows[0].token == body["token"]
    assert rows[0].user_id == "u-01"
    assert rows[0].expires_at == body["expires_at"]


def test_session_expiry_follows_token_expiry(client, factory):
    body = _login(client)
    row = _sessions(factory)[0]

    assert abs(row.expires_at - (time.time() + JWT_ACCESS_TTL)) < 5
    assert row.expires_at == _payload(body["token"])["exp"]


def test_custom_ttl_is_applied_to_token_and_session(factory):
    user = {"id": "u-02", "username": "cust-02", "name": "客户小王", "role": "customer"}

    issued = auth_service.issue_login_token(user, ttl=120)

    assert issued["expires_in"] == 120
    assert _payload(issued["token"])["exp"] - _payload(issued["token"])["iat"] == 120
    assert _sessions(factory)[0].expires_at == issued["expires_at"]


# ---- 既有解析路径仍可用 ----


def test_me_endpoint_accepts_issued_token(client):
    token = _login(client)["token"]

    resp = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "cust-01"


def test_me_endpoint_rejects_invalid_token(client):
    resp = client.get("/api/me", headers={"Authorization": "Bearer not-a-token"})

    assert resp.json() == {"user": None}


# ---- 失败分支不改变契约 ----


def test_wrong_password_keeps_legacy_response_shape(client, factory):
    body = _login(client, "WrongPass@2026")

    assert body == {"token": "", "user": {}}
    assert _sessions(factory) == []


def test_unknown_user_issues_nothing(client, factory):
    body = client.post(
        "/api/login", json={"username": "nobody", "password": PASSWORD}
    ).json()

    assert body == {"token": "", "user": {}}
    assert _sessions(factory) == []
