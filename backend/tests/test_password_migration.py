"""旧密码哈希平滑迁移测试。

核心诉求：存量账号（密码字段还是旧版 sha256 摘要）不改密码也能登录，
并在首次登录成功时自动升级为 bcrypt；升级过程中出现的任何问题都不能
影响登录本身，也不能让账号再也登不进来。

测试使用独立临时 SQLite，不读写真实的 app.db。
"""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api.auth import auth_router
from backend.database import Base
from backend.models import User
from backend.models.user import PASSWORD_HASH_LENGTH
from backend.services import auth as auth_service
from backend.services.password import (
    MAX_PASSWORD_BYTES,
    PasswordHashError,
    hash_cost,
    hash_password,
    is_bcrypt_hash,
    verify_password,
)

PASSWORD = "OldPass@2024"
# 单份哈希成本压到最低档，避免测试被 bcrypt 计算拖慢
TEST_COST = 6


def _legacy_digest(password: str) -> str:
    """迁移前的旧存法：不加盐的 sha256 摘要。"""
    return hashlib.sha256(password.encode()).hexdigest()


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    """临时库 + 把认证服务的会话工厂指到临时库。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(auth_service, "SessionLocal", session_factory)
    monkeypatch.setenv("BCRYPT_COST", str(TEST_COST))
    return session_factory


def _add_user(factory, password_hash: str, username: str = "cust-01", role: str = "customer") -> str:
    db = factory()
    try:
        db.add(
            User(
                id="u-01",
                username=username,
                password_hash=password_hash,
                name="客户小李",
                role=role,
            )
        )
        db.commit()
    finally:
        db.close()
    return username


def _stored_hash(factory, username: str = "cust-01") -> str:
    db = factory()
    try:
        user = db.query(User).filter(User.username == username).first()
        return user.password_hash
    finally:
        db.close()


# ---- 迁移主路径 ----


def test_legacy_account_logs_in_and_gets_upgraded(factory):
    _add_user(factory, _legacy_digest(PASSWORD))
    assert not is_bcrypt_hash(_stored_hash(factory))

    account = auth_service.authenticate("cust-01", PASSWORD)

    assert account == {
        "id": "u-01",
        "username": "cust-01",
        "name": "客户小李",
        "role": "customer",
    }
    stored = _stored_hash(factory)
    assert is_bcrypt_hash(stored)
    assert verify_password(PASSWORD, stored)
    assert hash_cost(stored) == TEST_COST


def test_upgraded_account_can_log_in_again(factory):
    _add_user(factory, _legacy_digest(PASSWORD))
    auth_service.authenticate("cust-01", PASSWORD)
    first = _stored_hash(factory)

    account = auth_service.authenticate("cust-01", PASSWORD)

    assert account["username"] == "cust-01"
    # 已是同等强度的哈希，第二次登录不再重写字段
    assert _stored_hash(factory) == first


def test_upgrade_keeps_other_profile_fields(factory):
    _add_user(factory, _legacy_digest(PASSWORD), role="admin")

    account = auth_service.authenticate("cust-01", PASSWORD)

    assert account["role"] == "admin"
    db = factory()
    try:
        user = db.query(User).filter(User.username == "cust-01").first()
        assert (user.name, user.role, user.id) == ("客户小李", "admin", "u-01")
    finally:
        db.close()


def test_returned_account_has_no_password_hash(factory):
    _add_user(factory, _legacy_digest(PASSWORD))

    account = auth_service.authenticate("cust-01", PASSWORD)

    assert "password_hash" not in account
    assert PASSWORD not in str(account)


# ---- 不该升级的场景 ----


def test_wrong_password_does_not_upgrade(factory):
    legacy = _legacy_digest(PASSWORD)
    _add_user(factory, legacy)

    assert auth_service.authenticate("cust-01", "WrongPass@2024") is None
    assert _stored_hash(factory) == legacy


def test_unknown_account_returns_none(factory):
    assert auth_service.authenticate("nobody", PASSWORD) is None


@pytest.mark.parametrize("username,password", [("", PASSWORD), ("cust-01", ""), (None, PASSWORD), ("cust-01", 12345)])
def test_invalid_input_returns_none(factory, username, password):
    _add_user(factory, _legacy_digest(PASSWORD), username="cust-01")

    assert auth_service.authenticate(username, password) is None
    assert not is_bcrypt_hash(_stored_hash(factory))


def test_current_cost_hash_is_left_untouched(factory):
    stored = hash_password(PASSWORD, cost=TEST_COST)
    _add_user(factory, stored)

    assert auth_service.authenticate("cust-01", PASSWORD)
    assert _stored_hash(factory) == stored


# ---- 成本因子升级 ----


def test_low_cost_hash_is_rehashed_at_current_cost(factory):
    low = hash_password(PASSWORD, cost=4)
    _add_user(factory, low)

    assert auth_service.authenticate("cust-01", PASSWORD)

    upgraded = _stored_hash(factory)
    assert upgraded != low
    assert hash_cost(upgraded) == TEST_COST
    assert verify_password(PASSWORD, upgraded)


def test_hash_delivered_length_fits_column():
    bcrypt_hash = hash_password(PASSWORD, cost=4)
    assert len(bcrypt_hash) == PASSWORD_HASH_LENGTH == 60
    column = User.__table__.c.password_hash.type
    assert column.length == PASSWORD_HASH_LENGTH
    assert column.dialect_impl(mysql.dialect()).length >= len(_legacy_digest(PASSWORD))


# ---- 升级失败不影响登录 ----


def test_overlong_legacy_password_still_logs_in(factory):
    long_password = "p" * (MAX_PASSWORD_BYTES + 8)
    legacy = _legacy_digest(long_password)
    _add_user(factory, legacy)

    account = auth_service.authenticate("cust-01", long_password)

    assert account["username"] == "cust-01"
    # bcrypt 不接受超长输入，字段保持旧摘要，下次登录仍可继续尝试
    assert _stored_hash(factory) == legacy


def test_write_failure_does_not_block_login(factory, monkeypatch):
    _add_user(factory, _legacy_digest(PASSWORD))

    def boom(self, password):
        raise SQLAlchemyError("disk full")

    monkeypatch.setattr(User, "set_password", boom)

    account = auth_service.authenticate("cust-01", PASSWORD)

    assert account["username"] == "cust-01"
    assert not is_bcrypt_hash(_stored_hash(factory))


def test_hash_error_is_swallowed(factory, monkeypatch):
    _add_user(factory, _legacy_digest(PASSWORD))

    def boom(self, password):
        raise PasswordHashError("bad password")

    monkeypatch.setattr(User, "set_password", boom)

    assert auth_service.authenticate("cust-01", PASSWORD)


# ---- 登录接口联调 ----


@pytest.fixture()
def client(factory):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    return TestClient(app)


def test_login_endpoint_upgrades_legacy_hash(factory, client):
    _add_user(factory, _legacy_digest(PASSWORD))

    resp = client.post("/api/login", json={"username": "cust-01", "password": PASSWORD})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["user"] == {"id": "u-01", "username": "cust-01", "name": "客户小李", "role": "customer"}
    assert is_bcrypt_hash(_stored_hash(factory))


def test_login_endpoint_rejects_wrong_password_without_upgrade(factory, client):
    legacy = _legacy_digest(PASSWORD)
    _add_user(factory, legacy)

    resp = client.post("/api/login", json={"username": "cust-01", "password": "WrongPass@2024"})

    assert resp.status_code == 200
    assert resp.json() == {"token": "", "user": {}}
    assert _stored_hash(factory) == legacy


def test_me_endpoint_reads_upgraded_account(factory, client):
    _add_user(factory, _legacy_digest(PASSWORD))
    token = client.post("/api/login", json={"username": "cust-01", "password": PASSWORD}).json()["token"]

    resp = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "cust-01"
    assert is_bcrypt_hash(_stored_hash(factory))
