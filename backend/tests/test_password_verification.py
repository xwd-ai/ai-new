"""密码哈希与校验测试。

覆盖范围：
- 模型层：``User`` 的密码写入、比对与升级语义（bcrypt 与旧版摘要两条路径）；
- 服务层：``authenticate`` 的成功/失败分支，失败分支不泄露差异、不误改字段；
- 链路：注册写入哈希 → 校验通过 → 登录拿到会话，明文全程不落库、不返回。

测试使用独立临时 SQLite，不读写真实的 app.db。
"""

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import User
from backend.models.user import PASSWORD_HASH_LENGTH
from backend.services import auth as auth_service
from backend.services.password import (
    DEFAULT_COST,
    MAX_PASSWORD_BYTES,
    MIN_COST,
    hash_cost,
    hash_password,
    is_bcrypt_hash,
    verify_password,
)
from backend.services.auth import validate_password_strength

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
        f"sqlite:///{tmp_path / 'password.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(auth_service, "SessionLocal", session_factory)
    monkeypatch.setenv("BCRYPT_COST", str(TEST_COST))
    return session_factory


def _user(factory, password_hash: str = "", username: str = "cust-01") -> User:
    """在临时库里落一个账号，返回游离态对象供断言字段用。"""
    db = factory()
    try:
        user = User(
            id="u-01",
            username=username,
            password_hash=password_hash,
            name="客户小李",
            role="customer",
        )
        db.add(user)
        db.commit()
        return user
    finally:
        db.close()


def _stored_hash(factory, username: str = "cust-01") -> str:
    db = factory()
    try:
        return db.query(User).filter(User.username == username).first().password_hash
    finally:
        db.close()


# ---- 模型层：写入与比对 ----


def test_set_password_stores_bcrypt_of_fixed_length():
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")

    stored = user.set_password(PASSWORD)

    assert stored == user.password_hash
    assert is_bcrypt_hash(stored)
    assert len(stored) == PASSWORD_HASH_LENGTH
    assert PASSWORD not in stored


def test_check_password_matches_and_rejects():
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
    user.set_password(PASSWORD)

    assert user.check_password(PASSWORD) is True
    assert user.check_password("OldPass@2025") is False
    assert user.check_password("oldpass@2024") is False


def test_check_password_accepts_legacy_digest_while_migrating():
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
    user.password_hash = _legacy_digest(PASSWORD)

    assert user.is_bcrypt_password is False
    assert user.check_password(PASSWORD) is True
    assert user.check_password("OldPass@2025") is False


@pytest.mark.parametrize("stored", [None, "", "not-a-hash", "$2b$12$truncated"])
def test_check_password_rejects_unusable_stored_value(stored):
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
    user.password_hash = stored

    assert user.check_password(PASSWORD) is False


@pytest.mark.parametrize("password", [None, 12345, b"OldPass@2024", ""])
def test_check_password_tolerates_non_text_input(password):
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
    user.set_password(PASSWORD)

    assert user.check_password(password) is False


def test_check_password_tolerates_unencodable_input_on_legacy_digest():
    # 旧摘要分支需要对明文重新做摘要，含孤立代理字符的字符串无法编码
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
    user.password_hash = _legacy_digest(PASSWORD)

    assert user.check_password("\ud800") is False


# ---- 模型层：升级判定 ----


def test_password_needs_upgrade_flags():
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")

    user.password_hash = _legacy_digest(PASSWORD)
    assert user.password_needs_upgrade is True

    user.password_hash = hash_password(PASSWORD, cost=MIN_COST)
    assert user.password_needs_upgrade is True

    user.password_hash = hash_password(PASSWORD)
    assert user.password_needs_upgrade is False


def test_upgrade_password_only_after_verification():
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
    legacy = _legacy_digest(PASSWORD)
    user.password_hash = legacy

    assert user.upgrade_password("OldPass@2025") is False
    assert user.password_hash == legacy

    assert user.upgrade_password(PASSWORD) is True
    assert is_bcrypt_hash(user.password_hash)
    assert user.check_password(PASSWORD) is True


def test_upgrade_password_is_noop_for_current_hash():
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
    current = hash_password(PASSWORD)
    user.password_hash = current

    assert user.upgrade_password(PASSWORD) is False
    assert user.password_hash == current


# ---- 服务层：登录校验 ----


def test_authenticate_success_returns_public_fields_only(factory):
    user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
    user.set_password(PASSWORD)
    _user(factory, user.password_hash)

    account = auth_service.authenticate("cust-01", PASSWORD)

    assert account == {"id": "u-01", "username": "cust-01", "name": "客户小李", "role": "customer"}
    assert PASSWORD not in str(account)


def test_authenticate_mismatch_leaves_hash_untouched(factory):
    stored = hash_password(PASSWORD)
    _user(factory, stored)

    assert auth_service.authenticate("cust-01", "OldPass@2025") is None
    assert _stored_hash(factory) == stored


def test_authenticate_rejects_password_beyond_algorithm_window(factory):
    # 登录不得因超长密码抛异常：bcrypt 只处理前 72 字节，超长输入直接判否
    stored = hash_password(PASSWORD)
    _user(factory, stored)

    oversized = "p" * (MAX_PASSWORD_BYTES + 8)
    assert auth_service.authenticate("cust-01", oversized) is None
    assert _stored_hash(factory) == stored


def test_authenticate_rejects_unknown_account(factory):
    _user(factory, hash_password(PASSWORD))

    assert auth_service.authenticate("nobody", PASSWORD) is None


def test_placeholder_hash_is_a_real_bcrypt_hash():
    """账号不存在时用于空转校验的占位串必须是结构完整、成本一致的哈希。

    占位串格式失效会让这条分支提前返回，两条失败分支的耗时差异重新暴露。
    """
    placeholder = auth_service._PLACEHOLDER_HASH

    assert is_bcrypt_hash(placeholder)
    assert hash_cost(placeholder) == DEFAULT_COST
    assert verify_password(PASSWORD, placeholder) is False


# ---- 链路：注册写入哈希后可直接登录 ----


def test_registered_account_stores_bcrypt_and_can_log_in(factory):
    account = auth_service.register_user("cust-02", PASSWORD, name="客户小王")

    assert account["username"] == "cust-02"
    assert "password_hash" not in account

    stored = _stored_hash(factory, "cust-02")
    assert is_bcrypt_hash(stored)
    assert stored != PASSWORD
    assert verify_password(PASSWORD, stored)

    assert auth_service.authenticate("cust-02", PASSWORD)["id"] == account["id"]


def test_registration_salts_each_password(factory):
    auth_service.register_user("cust-02", PASSWORD)
    auth_service.register_user("cust-03", PASSWORD)

    assert _stored_hash(factory, "cust-02") != _stored_hash(factory, "cust-03")


@pytest.mark.parametrize(
    "password,violation",
    [
        ("Ab1", "长度至少"),
        ("p" * (MAX_PASSWORD_BYTES + 1), "字节"),
        ("abcd 1234", "空白"),
        ("abcdefgh", "字母与数字"),
        ("aaaaaaaa", "同一个字符的重复"),
        ("password1", "常见弱口令"),
    ],
)
def test_password_strength_rules_reported(password, violation):
    assert any(violation in item for item in validate_password_strength(password))


def test_password_strength_rejects_username_inside_password():
    problems = validate_password_strength("Cust01234x", "cust")

    assert any("用户名" in item for item in problems)


def test_password_strength_accepts_compliant_password():
    assert validate_password_strength(PASSWORD, "cust-01") == []


# ---- 接口层 ----
