"""密码哈希工具测试：生成格式、校验语义、成本因子配置与异常边界。"""

import hashlib

import pytest

from backend.services.password import (
    DEFAULT_COST,
    MAX_COST,
    MAX_PASSWORD_BYTES,
    MIN_COST,
    PasswordHashError,
    hash_cost,
    hash_password,
    is_bcrypt_hash,
    needs_rehash,
    resolve_cost,
    verify_password,
)

PASSWORD = "Pro@2026-customer"


def test_hash_returns_bcrypt_string():
    stored = hash_password(PASSWORD)

    assert is_bcrypt_hash(stored)
    assert len(stored) == 60
    assert stored != PASSWORD
    assert PASSWORD not in stored


def test_verify_accepts_correct_password():
    stored = hash_password(PASSWORD)

    assert verify_password(PASSWORD, stored) is True


def test_verify_rejects_wrong_password():
    stored = hash_password(PASSWORD)

    assert verify_password("pro@2026-customer", stored) is False
    assert verify_password(PASSWORD + " ", stored) is False
    assert verify_password("", stored) is False


def test_same_password_gets_different_salt():
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert first != second
    assert verify_password(PASSWORD, first)
    assert verify_password(PASSWORD, second)


@pytest.mark.parametrize(
    "password",
    ["a", "李女士123", "emoji-🔐-密码", "x" * MAX_PASSWORD_BYTES],
)
def test_various_passwords_roundtrip(password):
    assert verify_password(password, hash_password(password))


def test_multibyte_password_length_limit():
    # 单个汉字 3 字节，24 个汉字正好 72 字节，25 个汉字超出
    ok = "密" * (MAX_PASSWORD_BYTES // 3)
    assert verify_password(ok, hash_password(ok))

    with pytest.raises(PasswordHashError):
        hash_password("密" * (MAX_PASSWORD_BYTES // 3 + 1))


def test_empty_and_non_string_password_rejected():
    with pytest.raises(PasswordHashError):
        hash_password("")
    with pytest.raises(PasswordHashError):
        hash_password(None)
    with pytest.raises(PasswordHashError):
        hash_password(12345678)


def test_explicit_cost_is_recorded_in_hash():
    stored = hash_password(PASSWORD, cost=MIN_COST)

    assert hash_cost(stored) == MIN_COST
    assert verify_password(PASSWORD, stored)


def test_cost_from_env(monkeypatch):
    monkeypatch.setenv("BCRYPT_COST", "6")

    assert resolve_cost() == 6
    assert hash_cost(hash_password(PASSWORD)) == 6


def test_cost_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("BCRYPT_COST", raising=False)

    assert resolve_cost() == DEFAULT_COST
    assert hash_cost(hash_password(PASSWORD)) == DEFAULT_COST


@pytest.mark.parametrize("raw", ["", "abc", "-3", "1.5"])
def test_invalid_env_cost_uses_default(monkeypatch, raw):
    monkeypatch.setenv("BCRYPT_COST", raw)

    assert resolve_cost() == DEFAULT_COST


def test_cost_out_of_range_is_clamped(monkeypatch):
    monkeypatch.setenv("BCRYPT_COST", "99")
    assert resolve_cost() == MAX_COST

    monkeypatch.setenv("BCRYPT_COST", "1")
    assert resolve_cost() == MIN_COST

    assert resolve_cost(99) == MAX_COST
    assert resolve_cost(1) == MIN_COST


def test_legacy_digest_is_not_bcrypt():
    legacy = hashlib.sha256(PASSWORD.encode()).hexdigest()

    assert is_bcrypt_hash(legacy) is False
    assert hash_cost(legacy) is None
    assert verify_password(PASSWORD, legacy) is False


@pytest.mark.parametrize("stored", [None, "", "not-a-hash", "$2b$12$", "sha256:abc"])
def test_verify_tolerates_malformed_stored(stored):
    assert is_bcrypt_hash(stored) is False
    assert verify_password(PASSWORD, stored) is False


def test_needs_rehash():
    low = hash_password(PASSWORD, cost=MIN_COST)

    assert needs_rehash(low, cost=MIN_COST + 2) is True
    assert needs_rehash(low, cost=MIN_COST) is False
    assert needs_rehash(hash_password(PASSWORD, cost=MAX_COST), cost=MIN_COST) is False
    assert needs_rehash(hashlib.sha256(PASSWORD.encode()).hexdigest()) is True
    assert needs_rehash(None) is True


def test_hash_password_does_not_mutate_input():
    original = PASSWORD
    hash_password(PASSWORD)

    assert PASSWORD == original
