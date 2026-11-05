"""JWT 签发与校验测试。

签发侧与校验侧要能对得上，也要各自守得住：
- 签发结果的结构、声明与签名可独立复算，同样的输入得到同样的令牌；
- 校验按「结构 → 算法 → 签名 → 声明 → 时效」逐层收口，每一层的失败都落在
  稳定的错误码上，调用方可以据此分支，而拿不到密钥、签名原文这类内部信息；
- 篡改载荷、更换密钥、算法降级、声明缺失、超时未生效都必须被拒；
- 过渡期的旧会话令牌不受影响，签名令牌也不会退化成查库解析。

用例只读配置、不写真实 app.db；需要回库取用户的场景使用临时 SQLite。
"""

import base64
import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import User
from backend.services import auth as auth_service
from backend.services.auth import (
    JWT_ACCESS_TTL,
    JWT_ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_MAX_TOKEN_LENGTH,
    JWT_REQUIRED_CLAIMS,
    TOKEN_TYPE_ACCESS,
    TokenAlgorithmError,
    TokenClaimsError,
    TokenError,
    TokenExpiredError,
    TokenMalformedError,
    TokenMissingError,
    TokenNotYetValidError,
    TokenSignatureError,
    TokenSigningError,
)

SECRET = b"unit-test-signing-secret-0123456789abcdef"
OTHER_SECRET = b"another-signing-secret-fedcba9876543210xyz"

USER = {"id": "u-01", "username": "cust-01", "name": "客户小李", "role": "customer"}


@pytest.fixture(autouse=True)
def jwt_env(monkeypatch):
    """把密钥固定下来并清掉可能影响时效的配置，保证用例可复现。"""
    monkeypatch.setenv(auth_service.JWT_SECRET_ENV, SECRET.decode())
    monkeypatch.delenv(auth_service.JWT_TTL_ENV, raising=False)
    monkeypatch.delenv(auth_service.JWT_LEEWAY_ENV, raising=False)


@pytest.fixture()
def session_local(tmp_path, monkeypatch):
    """临时库并挂一个用户，供按 sub 回库取用户的用例使用。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jwt-verify.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    monkeypatch.setenv("BCRYPT_COST", "6")
    db = factory()
    try:
        user = User(id="u-01", username="cust-01", name="客户小李", role="customer")
        user.set_password("VerifyPass@2026")
        db.add(user)
        db.commit()
    finally:
        db.close()
    return factory


# ---- 编解码辅助：独立于被测实现，用来复算结构 ----


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode_segment(segment: str) -> dict:
    return json.loads(_unb64url(segment))


def _header(token: str) -> dict:
    return _decode_segment(token.split(".")[0])


def _claims(token: str) -> dict:
    """取出载荷声明；仅用于断言内容，不做签名校验。"""
    return _decode_segment(token.split(".")[1])


def _signature(token: str) -> bytes:
    return _unb64url(token.split(".")[2])


def _expected_signature(token: str, secret: bytes = SECRET) -> bytes:
    header_segment, payload_segment, _ = token.split(".")
    return hmac.new(
        secret,
        f"{header_segment}.{payload_segment}".encode("ascii"),
        hashlib.sha256,
    ).digest()


def _claims_at(now: int, ttl: int = 600, **overrides) -> dict:
    """按当前时刻构造一份完整声明，便于精确控制时效边界。"""
    claims = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": "u-01",
        "username": "cust-01",
        "role": "customer",
        "typ": TOKEN_TYPE_ACCESS,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": "fixed-jti-for-test",
    }
    claims.update(overrides)
    return claims


def _token(claims: dict, secret: bytes = SECRET, header: dict = None) -> str:
    """用给定声明签成令牌（签名有效，声明与头部可任意）。"""
    head = {"alg": JWT_ALGORITHM, "typ": "JWT"} if header is None else header
    header_segment = _b64url(json.dumps(head, sort_keys=True).encode())
    payload_segment = _b64url(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{header_segment}.{payload_segment}"
    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _token_from_segments(header_segment: str, payload_segment: str, secret: bytes = SECRET) -> str:
    """直接拼接两段内容并签名，用于构造解码阶段就失败的载荷。"""
    signing_input = f"{header_segment}.{payload_segment}"
    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


# ---- 签发：结构与可复现性 ----


def test_header_declares_hs256_without_padding():
    token = auth_service.sign_jwt({"sub": "u-01"})

    assert _header(token) == {"alg": "HS256", "typ": "JWT"}
    for segment in token.split("."):
        assert "=" not in segment


def test_signature_matches_independent_hmac():
    token = auth_service.sign_jwt({"sub": "u-01", "exp": 1})

    assert _signature(token) == _expected_signature(token)


def test_same_payload_yields_identical_token():
    payload = {"sub": "u-01", "role": "customer", "jti": "fixed"}

    assert auth_service.sign_jwt(payload) == auth_service.sign_jwt(payload)


def test_payload_argument_is_not_mutated():
    payload = {"sub": "u-01", "role": "customer"}
    snapshot = dict(payload)

    auth_service.sign_jwt(payload)

    assert payload == snapshot


def test_signature_changes_with_secret():
    payload = {"sub": "u-01"}

    default_token = auth_service.sign_jwt(payload)
    other_token = auth_service.sign_jwt(payload, secret=OTHER_SECRET)

    assert default_token.split(".")[1] == other_token.split(".")[1]
    assert _signature(default_token) != _signature(other_token)


# ---- 签发：access 载荷 ----


def test_access_payload_carries_all_required_claims():
    payload = auth_service.build_access_payload(USER, ttl=600, issued_at=1_700_000_000)

    for name in JWT_REQUIRED_CLAIMS:
        assert payload[name] not in (None, "")
    assert payload["iss"] == JWT_ISSUER
    assert payload["aud"] == JWT_AUDIENCE
    assert payload["sub"] == "u-01"
    assert payload["username"] == "cust-01"
    assert payload["role"] == "customer"
    assert payload["typ"] == TOKEN_TYPE_ACCESS
    assert payload["iat"] == payload["nbf"] == 1_700_000_000
    assert payload["exp"] - payload["iat"] == 600


def test_default_ttl_follows_current_config():
    payload = auth_service.build_access_payload(USER)

    assert payload["exp"] - payload["iat"] == JWT_ACCESS_TTL


def test_configured_ttl_is_used_when_argument_absent(monkeypatch):
    monkeypatch.setenv(auth_service.JWT_TTL_ENV, "600")

    payload = auth_service.build_access_payload(USER)

    assert payload["exp"] - payload["iat"] == 600


def test_jti_is_unique_per_issue():
    first = auth_service.build_access_payload(USER)
    second = auth_service.build_access_payload(USER)

    assert first["jti"] != second["jti"]
    assert len(first["jti"]) == 32


def test_missing_role_falls_back_to_customer():
    payload = auth_service.build_access_payload({"id": "u-02", "username": "cust-02"})

    assert payload["role"] == "customer"


@pytest.mark.parametrize(
    "user",
    [
        {},
        {"id": "u-01"},
        {"username": "cust-01"},
        {"id": "", "username": "cust-01"},
        {"id": "u-01", "username": ""},
        "u-01",
        None,
    ],
)
def test_incomplete_user_is_rejected(user):
    with pytest.raises(TokenSigningError) as excinfo:
        auth_service.build_access_payload(user)

    assert excinfo.value.error_code == "AUTH_TOKEN_SIGNING_FAILED"


@pytest.mark.parametrize("ttl", [0, -1, 1.5, True, "600"])
def test_invalid_ttl_is_rejected(ttl):
    with pytest.raises(TokenSigningError):
        auth_service.build_access_payload(USER, ttl=ttl)


# ---- 校验：正常路径 ----


def test_round_trip_returns_same_claims():
    payload = auth_service.build_access_payload(USER, ttl=600, issued_at=int(time.time()))

    claims = auth_service.decode_jwt(auth_service.sign_jwt(payload))

    assert claims == payload


def test_access_token_verifier_accepts_access_token():
    token = auth_service.create_access_token(USER)

    claims = auth_service.verify_access_token(token)

    assert claims["sub"] == "u-01"
    assert claims["typ"] == TOKEN_TYPE_ACCESS


def test_audience_may_be_a_list():
    token = _token(_claims_at(int(time.time()), aud=["other-web", JWT_AUDIENCE]))

    assert auth_service.decode_jwt(token)["aud"] == ["other-web", JWT_AUDIENCE]


def test_unknown_claim_is_preserved():
    token = _token(_claims_at(int(time.time()), scope="ticket:read"))

    assert auth_service.decode_jwt(token)["scope"] == "ticket:read"


# ---- 校验：结构层 ----


@pytest.mark.parametrize("token", ["", "   ", None, 123])
def test_missing_token_is_rejected(token):
    with pytest.raises(TokenMissingError):
        auth_service.decode_jwt(token)


@pytest.mark.parametrize(
    "token",
    [
        "only-one-segment",
        "two.segments",
        "four.seg.ments.here",
    ],
)
def test_wrong_segment_count_is_rejected(token):
    with pytest.raises(TokenMalformedError):
        auth_service.decode_jwt(token)


def test_illegal_characters_are_rejected():
    with pytest.raises(TokenMalformedError):
        auth_service.decode_jwt("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1LTAxIn0.a!b")


def test_oversized_token_is_rejected():
    with pytest.raises(TokenMalformedError):
        auth_service.decode_jwt("a" * (JWT_MAX_TOKEN_LENGTH + 1))


@pytest.mark.parametrize(
    "segment",
    [
        _b64url(b"not-json"),
        _b64url(b"[1, 2, 3]"),
        _b64url(b"\xff\xfe\x00"),
    ],
)
def test_unparsable_payload_is_rejected(segment):
    token = _token_from_segments(_b64url(b'{"alg":"HS256"}'), segment)

    with pytest.raises(TokenMalformedError):
        auth_service.decode_jwt(token)


# ---- 校验：算法层 ----


@pytest.mark.parametrize("algorithm", ["none", "None", "RS256", "HS384", "", None])
def test_unsupported_algorithm_is_rejected(algorithm):
    now = int(time.time())
    token = _token(_claims_at(now), header={"alg": algorithm, "typ": "JWT"})

    with pytest.raises(TokenAlgorithmError) as excinfo:
        auth_service.decode_jwt(token)

    assert excinfo.value.detail == {"expected": "HS256"}


def test_lowercase_algorithm_name_still_matches():
    now = int(time.time())
    token = _token(_claims_at(now), header={"alg": "hs256", "typ": "JWT"})

    assert auth_service.decode_jwt(token)["sub"] == "u-01"


# ---- 校验：签名层 ----


def test_tampered_payload_is_rejected():
    token = _token(_claims_at(int(time.time())))
    header_segment, payload_segment, signature_segment = token.split(".")
    forged_payload = _b64url(json.dumps(_claims_at(int(time.time()), role="admin")).encode())

    with pytest.raises(TokenSignatureError):
        auth_service.decode_jwt(f"{header_segment}.{forged_payload}.{signature_segment}")


def test_token_signed_with_other_secret_is_rejected():
    token = _token(_claims_at(int(time.time())), secret=OTHER_SECRET)

    with pytest.raises(TokenSignatureError):
        auth_service.decode_jwt(token)


def test_rotating_configured_secret_invalidates_old_tokens(monkeypatch):
    monkeypatch.delenv(auth_service.JWT_SECRET_ENV, raising=False)
    token = auth_service.create_access_token(USER)

    monkeypatch.setenv(auth_service.JWT_SECRET_ENV, OTHER_SECRET.decode())

    with pytest.raises(TokenSignatureError):
        auth_service.decode_jwt(token)


def test_garbage_signature_is_rejected():
    header_segment, payload_segment, _ = _token(_claims_at(int(time.time()))).split(".")

    with pytest.raises((TokenSignatureError, TokenMalformedError)):
        auth_service.decode_jwt(f"{header_segment}.{payload_segment}.@@@")


# ---- 校验：声明层 ----


@pytest.mark.parametrize("name", JWT_REQUIRED_CLAIMS)
def test_missing_required_claim_is_rejected(name):
    claims = _claims_at(int(time.time()))
    claims.pop(name)

    with pytest.raises(TokenClaimsError) as excinfo:
        auth_service.decode_jwt(_token(claims))

    assert excinfo.value.detail == {"claim": name}


def test_wrong_issuer_is_rejected():
    token = _token(_claims_at(int(time.time()), iss="another-service"))

    with pytest.raises(TokenClaimsError) as excinfo:
        auth_service.decode_jwt(token)

    assert excinfo.value.detail == {"claim": "iss"}


def test_wrong_audience_is_rejected():
    token = _token(_claims_at(int(time.time()), aud="other-web"))

    with pytest.raises(TokenClaimsError) as excinfo:
        auth_service.decode_jwt(token)

    assert excinfo.value.detail == {"claim": "aud"}


def test_non_access_token_type_is_rejected():
    token = _token(_claims_at(int(time.time()), typ="refresh"))

    with pytest.raises(TokenClaimsError) as excinfo:
        auth_service.verify_access_token(token)

    assert excinfo.value.detail == {"claim": "typ"}


def test_expected_checks_can_be_skipped():
    now = int(time.time())
    token = _token(_claims_at(now, iss="another-service", aud="other-web", typ="refresh"))

    claims = auth_service.decode_jwt(
        token, expected_issuer=None, expected_audience=None, expected_type=None
    )

    assert claims["iss"] == "another-service"


def test_exp_not_after_iat_is_rejected():
    now = int(time.time())
    token = _token(_claims_at(now, iat=now, exp=now))

    with pytest.raises(TokenClaimsError) as excinfo:
        auth_service.decode_jwt(token)

    assert excinfo.value.detail == {"claim": "exp"}


@pytest.mark.parametrize("name", ["iat", "exp", "nbf"])
def test_non_integer_timestamp_is_rejected(name):
    now = int(time.time())
    token = _token(_claims_at(now, **{name: "1700000000"}))

    with pytest.raises(TokenClaimsError) as excinfo:
        auth_service.decode_jwt(token)

    assert excinfo.value.detail == {"claim": name}


def test_boolean_timestamp_is_rejected():
    now = int(time.time())
    token = _token(_claims_at(now, exp=True))

    with pytest.raises(TokenClaimsError):
        auth_service.decode_jwt(token)


# ---- 校验：时效层 ----


def test_expired_token_is_rejected():
    now = int(time.time())
    token = _token(_claims_at(now, iat=now - 3600, nbf=now - 3600, exp=now - 600))

    with pytest.raises(TokenExpiredError) as excinfo:
        auth_service.decode_jwt(token, now=now)

    assert excinfo.value.error_code == "AUTH_TOKEN_EXPIRED"
    assert excinfo.value.detail == {"expired_at": now - 600}


def test_clock_leeway_tolerates_recent_expiry():
    now = int(time.time())
    token = _token(_claims_at(now, iat=now - 600, nbf=now - 600, exp=now - 10))

    assert auth_service.decode_jwt(token, now=now, leeway=30)["sub"] == "u-01"


def test_zero_leeway_rejects_recent_expiry():
    now = int(time.time())
    token = _token(_claims_at(now, iat=now - 600, nbf=now - 600, exp=now - 10))

    with pytest.raises(TokenExpiredError):
        auth_service.decode_jwt(token, now=now, leeway=0)


def test_token_not_yet_valid_is_rejected():
    now = int(time.time())
    token = _token(_claims_at(now, iat=now + 120, nbf=now + 120, exp=now + 1800))

    with pytest.raises(TokenNotYetValidError) as excinfo:
        auth_service.decode_jwt(token, now=now, leeway=0)

    assert excinfo.value.detail == {"not_before": now + 120}


def test_expiry_check_can_be_skipped_for_diagnostics():
    now = int(time.time())
    token = _token(_claims_at(now, iat=now - 3600, nbf=now - 3600, exp=now - 600))

    claims = auth_service.decode_jwt(token, now=now, verify_expiry=False)

    assert claims["exp"] == now - 600


def test_tampered_expired_token_reports_signature_first():
    now = int(time.time())
    token = _token(_claims_at(now, iat=now - 3600, nbf=now - 3600, exp=now - 600), secret=OTHER_SECRET)

    with pytest.raises(TokenSignatureError):
        auth_service.decode_jwt(token, now=now)


def test_invalid_leeway_argument_is_rejected():
    token = _token(_claims_at(int(time.time())))

    with pytest.raises(ValueError):
        auth_service.decode_jwt(token, leeway=-1)


# ---- 签名预检：布尔返回，不抛异常 ----


def test_verify_jwt_signature_returns_boolean():
    token = _token(_claims_at(int(time.time())))

    assert auth_service.verify_jwt_signature(token) is True
    assert auth_service.verify_jwt_signature(token, secret=OTHER_SECRET) is False
    assert auth_service.verify_jwt_signature("not-a-token") is False
    assert auth_service.verify_jwt_signature("") is False


def test_verify_jwt_signature_ignores_expiry():
    now = int(time.time())
    token = _token(_claims_at(now, ttl=-600))

    assert auth_service.verify_jwt_signature(token) is True


# ---- Authorization 头解析 ----


@pytest.mark.parametrize("header", ["Bearer abc.def.ghi", "bearer abc.def.ghi", "Bearer   abc.def.ghi  "])
def test_extract_bearer_token_accepts_standard_header(header):
    assert auth_service.extract_bearer_token(header) == "abc.def.ghi"


@pytest.mark.parametrize("header", [None, "", "   "])
def test_extract_bearer_token_requires_header(header):
    with pytest.raises(TokenMissingError):
        auth_service.extract_bearer_token(header)


@pytest.mark.parametrize("header", ["Bearer", "Bearer   ", "Basic abc", "Token abc"])
def test_extract_bearer_token_rejects_bad_shape(header):
    with pytest.raises((TokenMissingError, TokenMalformedError)):
        auth_service.extract_bearer_token(header)


# ---- 身份解析：签名令牌以声明为准，失败回落为未登录 ----


def test_resolve_token_returns_user_from_database(session_local):
    token = auth_service.create_access_token(USER)

    user = auth_service.resolve_token(token)

    assert user == {"id": "u-01", "username": "cust-01", "name": "客户小李", "role": "customer"}


def test_resolve_authorization_accepts_bearer_and_raw_token(session_local):
    token = auth_service.create_access_token(USER)

    assert auth_service.resolve_authorization(f"Bearer {token}")["id"] == "u-01"
    assert auth_service.resolve_authorization(token)["id"] == "u-01"


def test_resolve_token_returns_none_for_expired_token(session_local):
    now = int(time.time())
    token = _token(_claims_at(now, iat=now - 3600, nbf=now - 3600, exp=now - 1800))

    assert auth_service.resolve_token(token) is None


def test_resolve_token_returns_none_for_unknown_subject(session_local):
    token = _token(_claims_at(int(time.time()), sub="u-missing"))

    assert auth_service.resolve_token(token) is None


def test_resolve_token_returns_none_for_forged_signature(session_local):
    token = _token(_claims_at(int(time.time())), secret=OTHER_SECRET)

    assert auth_service.resolve_token(token) is None


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer garbage"])
def test_resolve_authorization_returns_none_on_bad_header(session_local, header):
    assert auth_service.resolve_authorization(header) is None


def test_token_errors_share_a_base_class():
    """接口层按基类统一转 401，子类只用来区分失败环节。"""
    for error in (
        TokenMissingError,
        TokenMalformedError,
        TokenAlgorithmError,
        TokenSignatureError,
        TokenExpiredError,
        TokenNotYetValidError,
        TokenClaimsError,
    ):
        assert issubclass(error, TokenError)
        assert error.http_status == 401
