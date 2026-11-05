"""认证与会话服务。

用户查询、注册、密码校验与会话签发都在这一层收口：
- 用户信息从 users 表读取，对外只暴露 id / username / name / role，密码哈希不出本层；
- 注册时校验用户名格式与密码强度，通过后写入 bcrypt 哈希，明文不落库；
- 密码比对委托 User.check_password：bcrypt 哈希按算法校验，仍为旧版摘要的账号按兼容逻辑比对；
- 校验通过的账号若仍存旧版摘要，或哈希成本低于当前配置，趁这次登录把字段原地升级，
  存量用户无需重置密码即可完成迁移；
- 登录成功后签发 JWT（HS256 紧凑格式，载荷只带鉴权必需的声明），
  同时把该令牌登记到 sessions 表：既有解析路径按令牌原文查会话，
  在令牌校验链路接管前保持兼容，令牌到期时间与会话记录保持一致；
- 令牌校验按「结构 → 算法 → 签名 → 声明」四层逐级收口，任何一层不通过都抛
  TokenError 派生异常，错误码稳定可供客户端分支，校验本身不查库、不依赖会话表；
- 有效期、时钟偏差与签名密钥都来自环境变量（AI_NEW_JWT_TTL / AI_NEW_JWT_LEEWAY /
  AI_NEW_JWT_SECRET），调用时解析而非 import 时定死；配置缺失或取值非法时
  告警并回退默认值，不会因为一处配置写错就让服务起不来；
- 接口层取当前用户统一走 resolve_authorization：签名令牌校验通过后按 sub 回库取
  用户，用户信息始终以库为准；单段旧会话令牌在过渡期内仍可解析，不受影响。

明文密码只作为 authenticate / register_user 的入参存在，不落库、不进日志、不随用户信息返回。
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.database import SessionLocal
from backend.models import Session, User
from backend.services.password import MAX_PASSWORD_BYTES, PasswordHashError, verify_password

logger = logging.getLogger(__name__)

# 对外可见的用户字段；密码哈希不在此列，避免随用户信息流到接口与页面
_PUBLIC_FIELDS = ("id", "username", "name", "role")

# 账号不存在时用于消耗一次哈希校验的占位串（cost 与默认成本一致，明文不参与比对），
# 使「账号不存在」与「密码错误」两条失败分支耗时接近，不借响应速度探测账号
_PLACEHOLDER_HASH = "$2b$12$sEi4W.uy8dqAnA/yZybMn.5v344bf3eEqrCiF6wyb0p2AWAQFWIIu"

# 用户名：3–32 位，字母开头，其余可为字母、数字、下划线、点、连字符
USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,31}$")

# 密码策略：至少 8 位、同时含字母与数字、不含空白、不与用户名相关、不在常见弱口令内。
# 上限取 bcrypt 的处理窗口（72 字节），超出部分算法不参与计算，故直接拒绝而不静默截断。
PASSWORD_MIN_LENGTH = 8

# 显示名上限，超出部分截断（名称只是展示用，不必因此拒绝整个注册）
NAME_MAX_LENGTH = 32

# 常见弱口令：命中即拒绝，避免撞库成本过低的账号进入系统
_WEAK_PASSWORDS = frozenset(
    {
        "12345678", "123456789", "1234567890", "11111111", "88888888",
        "password", "password1", "passw0rd", "p@ssw0rd", "qwerty123",
        "abc12345", "a1234567", "admin123", "iloveyou", "letmein1",
    }
)


# ---- 业务异常：由接口层统一转成 {error, error_code, error_detail?} 响应 ----


class AuthError(ValueError):
    """认证业务异常基类，携带错误码与建议的 HTTP 状态。"""

    error_code = "AUTH_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_payload(self) -> dict:
        payload = {"error": self.message, "error_code": self.error_code}
        if self.detail:
            payload["error_detail"] = self.detail
        return payload


class InvalidUsernameError(AuthError):
    """用户名格式不符合规则。"""

    error_code = "AUTH_INVALID_USERNAME"
    http_status = 422


class UsernameTakenError(AuthError):
    """用户名已被占用（比较忽略大小写）。"""

    error_code = "AUTH_USERNAME_TAKEN"
    http_status = 409


class WeakPasswordError(AuthError):
    """密码未达到强度要求，detail 里逐条列出未通过的规则。"""

    error_code = "AUTH_WEAK_PASSWORD"
    http_status = 422


class TokenSigningError(AuthError):
    """令牌签发失败：用户信息缺少签发所需的字段。"""

    error_code = "AUTH_TOKEN_SIGNING_FAILED"
    http_status = 500


# ---- 令牌校验异常：统一 401，按 error_code 区分失败环节 ----
#
# 具体原因只回给客户端一个稳定的错误码与简短说明，不暴露密钥、签名原文
# 或内部分支，避免把校验细节变成试探令牌构造的线索。


class TokenError(AuthError):
    """令牌校验失败基类。"""

    error_code = "AUTH_TOKEN_INVALID"
    http_status = 401


class TokenMissingError(TokenError):
    """请求未携带令牌。"""

    error_code = "AUTH_TOKEN_MISSING"


class TokenMalformedError(TokenError):
    """令牌结构不合法：段数不对、编码不可解码、头部或载荷不是 JSON 对象。"""

    error_code = "AUTH_TOKEN_MALFORMED"


class TokenAlgorithmError(TokenError):
    """头部声明的签名算法不受支持（含 alg=none 这类降级尝试）。"""

    error_code = "AUTH_TOKEN_UNSUPPORTED_ALG"


class TokenSignatureError(TokenError):
    """签名与载荷不匹配：令牌被篡改或由其它密钥签发。"""

    error_code = "AUTH_TOKEN_BAD_SIGNATURE"


class TokenExpiredError(TokenError):
    """令牌已过期。"""

    error_code = "AUTH_TOKEN_EXPIRED"


class TokenNotYetValidError(TokenError):
    """令牌尚未生效：nbf 或 iat 晚于当前时间，超出允许的时钟偏差。"""

    error_code = "AUTH_TOKEN_NOT_YET_VALID"


class TokenClaimsError(TokenError):
    """声明缺失或取值不符合约定：签发方、使用方、类型、时间戳。"""

    error_code = "AUTH_TOKEN_BAD_CLAIMS"


# ---- 注册：用户名与密码强度校验 ----


def normalize_username(username: str) -> str:
    """去首尾空白并校验用户名格式，返回可直接落库的用户名。

    格式不符时抛 InvalidUsernameError，由接口层转成 422。
    """
    if not isinstance(username, str):
        raise InvalidUsernameError("用户名必须是文本")
    value = username.strip()
    if not USERNAME_PATTERN.match(value):
        raise InvalidUsernameError(
            "用户名需 3–32 位，以字母开头，仅可含字母、数字、下划线、点或连字符"
        )
    return value


def validate_password_strength(password: str, username: str = "") -> list[str]:
    """按密码策略逐条检查，返回未通过的规则说明；空列表表示通过。

    纯判定函数，不写库、不抛异常，便于在注册之外（如改密）复用，
    也便于把「哪几条没达标」原样返回给前端逐项提示。
    """
    if not isinstance(password, str) or not password:
        return ["密码不能为空"]

    problems: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"长度至少 {PASSWORD_MIN_LENGTH} 位")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        problems.append(f"长度不能超过 {MAX_PASSWORD_BYTES} 字节")
    if any(ch.isspace() for ch in password):
        problems.append("不能包含空格等空白字符")
    has_letter = any(ch.isalpha() for ch in password)
    has_digit = any(ch.isdigit() for ch in password)
    if not (has_letter and has_digit):
        problems.append("需同时包含字母与数字")
    if len(set(password)) == 1:
        problems.append("不能是同一个字符的重复")
    if password.lower() in _WEAK_PASSWORDS:
        problems.append("不能使用常见弱口令")

    account = (username or "").strip().lower()
    if len(account) >= 3 and account in password.lower():
        problems.append("不能包含用户名")
    return problems


def _username_exists(db, username: str) -> bool:
    """用户名是否已被占用，比较时忽略大小写（Admin 与 admin 视为同名）。"""
    return (
        db.query(User).filter(func.lower(User.username) == username.lower()).first()
        is not None
    )


def register_user(username: str, password: str, name: str = "", role: str = "customer") -> dict:
    """创建账号并返回可公开的用户信息。

    校验顺序：用户名格式 → 密码强度 → 用户名占用，全部通过才写入。
    密码经 User.set_password 哈希后落库，明文不进入返回值与日志。
    role 由调用方决定，公开注册入口固定传 customer，不开放管理员身份的自助创建。
    """
    account_name = normalize_username(username)
    problems = validate_password_strength(password, account_name)
    if problems:
        raise WeakPasswordError("密码强度不足", {"violations": problems})

    db = SessionLocal()
    try:
        if _username_exists(db, account_name):
            raise UsernameTakenError("用户名已被占用")
        user = User(
            id=f"u-{uuid.uuid4().hex[:10]}",
            username=account_name,
            name=(name or "").strip()[:NAME_MAX_LENGTH] or account_name,
            role=role or "customer",
        )
        user.set_password(password)
        db.add(user)
        db.commit()
        return _account(user)
    except IntegrityError:
        # 预检查与写入之间仍可能被其它请求抢先插入，唯一约束兜住并统一按「已占用」返回
        db.rollback()
        raise UsernameTakenError("用户名已被占用") from None
    finally:
        db.close()


def _account(user: User) -> dict:
    """把用户记录转成对外字典。"""
    return {field: getattr(user, field) for field in _PUBLIC_FIELDS}


def _query_user(db, username: str) -> Optional[User]:
    """按用户名取用户记录，未命中返回 None。"""
    return db.query(User).filter(User.username == username).first()


def find_by_username(username: str) -> Optional[dict]:
    """按用户名查询用户信息，不存在返回 None。"""
    if not username:
        return None
    db = SessionLocal()
    try:
        user = _query_user(db, username)
        return _account(user) if user else None
    finally:
        db.close()


def _upgrade_password(db, user: User, password: str) -> bool:
    """登录成功后把旧强度密码字段升级为当前算法与成本，返回是否发生改写。

    只在字段确需升级时动手：旧版摘要、或 bcrypt 成本低于当前配置。
    升级是登录的附带动作，任何失败都只回滚本次写入、不影响登录结果，
    下一次登录会重新尝试，用户不会因为迁移失败而登不进来。
    """
    if not user.password_needs_upgrade:
        return False
    try:
        user.set_password(password)
        db.commit()
    except PasswordHashError:
        # 旧摘要允许超长密码，bcrypt 不接受超过 72 字节的输入；
        # 这类账号保持原状，登录本身仍然成功
        db.rollback()
        logger.warning("密码字段未升级：密码长度超出当前算法上限 username=%s", user.username)
        return False
    except SQLAlchemyError:
        db.rollback()
        logger.warning("密码字段升级写入失败 username=%s", user.username, exc_info=True)
        return False
    return True


def authenticate(username: str, password: str) -> Optional[dict]:
    """校验用户名与密码，通过返回用户信息，失败返回 None。

    账号不存在、密码不匹配、入参为空或类型不对，一律返回 None，
    调用方无法从返回值区分是哪一种失败。
    校验通过且密码字段仍是旧版摘要时，顺手把它升级为 bcrypt 哈希，
    实现存量账号首登自动迁移；升级结果不改变本次登录的成功与否。
    """
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    if not username or not password:
        return None

    db = SessionLocal()
    try:
        user = _query_user(db, username)
        if user is None:
            verify_password(password, _PLACEHOLDER_HASH)
            return None
        if not user.check_password(password):
            return None
        # 先取出对外字段，升级写库失败回滚后 ORM 对象会失效
        account = _account(user)
        _upgrade_password(db, user, password)
        return account
    finally:
        db.close()


# ---- JWT 签发：密钥、载荷与签名 ----
#
# 令牌用 JWS 紧凑格式（header.payload.signature），签名算法固定 HS256。
# 签名只依赖标准库的 HMAC-SHA256 与 base64url，不额外引入运行时依赖。
# 载荷只放鉴权必需的声明：身份信息靠 sub 回库取，令牌不承担用户快照的职责，
# 改名、改角色这类变更下次请求即生效，不必等令牌过期。
#
# 有效期、时钟偏差与签名密钥都取自环境变量，且都在调用时解析而不是 import 时定死：
# 换配置只需重启进程，不必改代码；同一进程内切换配置也能立刻生效，便于验证。

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "ai-new"
JWT_AUDIENCE = "ai-new-web"
JWT_ACCESS_TTL = 30 * 60  # access token 默认有效期：30 分钟
JWT_CLOCK_LEEWAY = 30  # 默认时钟偏差余量（秒）
JWT_SECRET_ENV = "AI_NEW_JWT_SECRET"
JWT_TTL_ENV = "AI_NEW_JWT_TTL"
JWT_LEEWAY_ENV = "AI_NEW_JWT_LEEWAY"
JWT_SECRET_MIN_BYTES = 32
TOKEN_TYPE_ACCESS = "access"

# 有效期允许区间：太短会让客户端频繁换发，太长则放大令牌泄露后的影响面
JWT_TTL_MIN = 60  # 1 分钟
JWT_TTL_MAX = 7 * 24 * 3600  # 7 天

# 时钟偏差上限：足够容忍正常的对时误差，又不至于让过期令牌长期可用
JWT_LEEWAY_MAX = 300

# 未配置环境变量时的本地开发密钥：固定值让重启后已签发的令牌仍可校验，
# 但只能用于本地调试，生产环境必须通过 JWT_SECRET_ENV 覆盖
_DEV_JWT_SECRET = "ai-new-dev-secret-change-me-before-deploy"

# 已读过的密钥按原文缓存编码结果：同一份配置只告警一次，重复签发不重复编码
_secret_cache: dict[str, bytes] = {}
_SECRET_CACHE_LIMIT = 8


def _env_text(name: str) -> str:
    """读取环境变量并去掉首尾空白，未设置或全为空白时返回空串。"""
    return (os.environ.get(name) or "").strip()


def _env_seconds(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取以秒为单位的配置项，取值不合法时告警并按默认值处理。

    只接受纯数字：写成 30m、1h 这类带单位的文本按非法值处理，
    避免换算口径在配置与代码之间对不齐。超出允许区间同样回退默认值，
    不让一次手误把令牌有效期放到不可控的量级。
    """
    raw = _env_text(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，按默认值 %d 秒处理", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "%s=%d 超出允许区间 [%d, %d]，按默认值 %d 秒处理",
            name,
            value,
            minimum,
            maximum,
            default,
        )
        return default
    return value


def jwt_access_ttl() -> int:
    """当前生效的 access token 有效期（秒）。"""
    return _env_seconds(JWT_TTL_ENV, JWT_ACCESS_TTL, JWT_TTL_MIN, JWT_TTL_MAX)


def jwt_clock_leeway() -> int:
    """当前生效的时钟偏差余量（秒）。"""
    return _env_seconds(JWT_LEEWAY_ENV, JWT_CLOCK_LEEWAY, 0, JWT_LEEWAY_MAX)


def get_jwt_secret() -> bytes:
    """读取签名密钥：优先环境变量，缺省回退开发密钥并给出告警。

    每次调用都重新读环境变量，因此改配置后下一次签发即生效；
    编码结果按密钥原文缓存，同一份配置不会被反复告警或反复编码。
    """
    raw = _env_text(JWT_SECRET_ENV)
    key = raw or _DEV_JWT_SECRET
    cached = _secret_cache.get(key)
    if cached is not None:
        return cached

    if not raw:
        logger.warning("未配置 %s，JWT 使用内置开发密钥，仅限本地调试", JWT_SECRET_ENV)
    elif len(key.encode("utf-8")) < JWT_SECRET_MIN_BYTES:
        logger.warning(
            "%s 不足 %d 字节，建议换成更长的随机密钥",
            JWT_SECRET_ENV,
            JWT_SECRET_MIN_BYTES,
        )

    secret = key.encode("utf-8")
    if len(_secret_cache) >= _SECRET_CACHE_LIMIT:
        _secret_cache.clear()
    _secret_cache[key] = secret
    return secret


def _b64url_encode(raw: bytes) -> str:
    """base64url 编码并去掉填充，符合 JWS 紧凑格式要求。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """base64url 解码，按需补齐被去掉的填充。"""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _compact_json(data: dict) -> bytes:
    """序列化为紧凑 JSON 字节串：无空格、键有序，同样的声明得到同样的令牌。"""
    return json.dumps(
        data, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")


def sign_jwt(payload: dict, secret: Optional[bytes] = None) -> str:
    """把声明集合签成紧凑格式令牌，返回 header.payload.signature 三段字符串。

    payload 只读，函数不修改入参；secret 缺省用模块级配置的密钥。
    """
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    segments = f"{_b64url_encode(_compact_json(header))}.{_b64url_encode(_compact_json(payload))}"
    signature = _sign(segments, get_jwt_secret() if secret is None else secret)
    return f"{segments}.{_b64url_encode(signature)}"


def build_access_payload(
    user: dict,
    ttl: Optional[int] = None,
    issued_at: Optional[int] = None,
) -> dict:
    """按 access token 的声明约定构造载荷。

    声明分工：
    iss/aud 限定签发方与使用方；sub 为用户 id，是回查用户的唯一依据；
    username/role 供鉴权与日志直接取用，不做权限判定以外的业务用途；
    iat/nbf/exp 为秒级时间戳，nbf 与 iat 取同一时刻，留出后续加宽时钟偏差的余地；
    jti 为一次性标识，为将来的吊销名单预留锚点；typ 区分令牌用途。

    ttl 不传时取当前配置的有效期，调用方显式给值则按给的算，
    便于为个别场景（如内部联调）签发短时令牌。
    """
    if not isinstance(user, dict) or not user.get("id") or not user.get("username"):
        raise TokenSigningError("用户信息缺少签发令牌所需的字段")

    lifetime = jwt_access_ttl() if ttl is None else ttl
    if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
        raise TokenSigningError("令牌有效期必须为正整数")

    now = int(time.time()) if issued_at is None else int(issued_at)
    return {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": user["id"],
        "username": user["username"],
        "role": user.get("role") or "customer",
        "typ": TOKEN_TYPE_ACCESS,
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }


def create_access_token(user: dict, ttl: Optional[int] = None) -> str:
    """为已通过校验的用户签发 access token。"""
    return sign_jwt(build_access_payload(user, ttl=ttl))


def describe_jwt_config() -> dict:
    """返回当前生效的签发参数，供健康检查与排障读取，不含密钥内容。

    只回密钥来源与长度是否达标，不回密钥本身，也不回具体长度，
    避免从配置信息反推出密钥规模。
    """
    return {
        "algorithm": JWT_ALGORITHM,
        "issuer": JWT_ISSUER,
        "audience": JWT_AUDIENCE,
        "access_ttl": jwt_access_ttl(),
        "clock_leeway": jwt_clock_leeway(),
        "secret_source": JWT_SECRET_ENV if _env_text(JWT_SECRET_ENV) else "dev-default",
        "secret_length_ok": len(get_jwt_secret()) >= JWT_SECRET_MIN_BYTES,
    }


# ---- JWT 解析与校验：结构、算法、签名、声明 ----
#
# 校验顺序刻意由外到内：先确认三段结构可解析，再看头部声明的算法，
# 然后比对签名，最后才读载荷里的声明。前一步不通过就不进入下一步，
# 保证「签名未验证的载荷」不会被当作可信数据读取或回显。
# 时效判定留出配置的时钟偏差余量，签发方与校验方时钟略有偏差不算过期。

JWT_MAX_TOKEN_LENGTH = 4096  # 令牌长度上限，超长直接拒绝，不做无谓解码
JWT_REQUIRED_CLAIMS = ("iss", "aud", "sub", "exp", "iat", "typ")

# base64url 段只允许这些字符；先按字符集筛一遍，避免解码器静默丢弃非法字符
_B64URL_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


def _split_token(token: str) -> tuple[str, str, str]:
    """把紧凑格式令牌拆成头部、载荷、签名三段，结构不合法时抛 TokenMalformedError。"""
    if not isinstance(token, str) or not token.strip():
        raise TokenMissingError("缺少认证令牌")
    value = token.strip()
    if len(value) > JWT_MAX_TOKEN_LENGTH:
        raise TokenMalformedError("令牌长度超出上限")
    parts = value.split(".")
    if len(parts) != 3:
        raise TokenMalformedError("令牌需为三段式结构")
    for segment in parts:
        if not _B64URL_SEGMENT.match(segment):
            raise TokenMalformedError("令牌包含非法字符")
    return parts[0], parts[1], parts[2]


def _decode_segment(segment: str, name: str) -> dict:
    """解码一个 base64url 段并解析为 JSON 对象。"""
    try:
        raw = _b64url_decode(segment)
        data = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise TokenMalformedError(f"令牌{name}无法解析") from None
    if not isinstance(data, dict):
        raise TokenMalformedError(f"令牌{name}不是 JSON 对象")
    return data


def _sign(segments: str, secret: bytes) -> bytes:
    """对 header.payload 段做 HMAC-SHA256 签名。"""
    return hmac.new(secret, segments.encode("ascii"), hashlib.sha256).digest()


def verify_jwt_signature(token: str, secret: Optional[bytes] = None) -> bool:
    """只比对签名，不判时效与声明，返回布尔值而不抛异常。

    适合在需要「先看令牌真伪、再决定要不要解析内容」的场景做前置过滤；
    结构与签名任何一处对不上都返回 False，调用方拿不到失败细节。
    """
    try:
        header_segment, payload_segment, signature_segment = _split_token(token)
        expected = _sign(
            f"{header_segment}.{payload_segment}",
            get_jwt_secret() if secret is None else secret,
        )
        actual = _b64url_decode(signature_segment)
    except (TokenError, binascii.Error, ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(expected, actual)


def _check_algorithm(header: dict) -> None:
    """头部算法必须与签发时一致；alg=none 之类的一律拒绝。"""
    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm.upper() != JWT_ALGORITHM:
        raise TokenAlgorithmError(
            "令牌签名算法不受支持", {"expected": JWT_ALGORITHM}
        )


def _require_claim(claims: dict, name: str) -> None:
    """声明必须存在且非空。"""
    if claims.get(name) in (None, ""):
        raise TokenClaimsError(f"令牌缺少声明 {name}", {"claim": name})


def _timestamp_claim(claims: dict, name: str) -> int:
    """取秒级时间戳声明；布尔值虽然也是 int 的子类，但不接受。"""
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TokenClaimsError(f"令牌声明 {name} 必须是秒级时间戳", {"claim": name})
    return value


def _audience_matches(claim_value, expected: str) -> bool:
    """aud 可以是单个字符串，也可以是字符串数组。"""
    if isinstance(claim_value, str):
        return claim_value == expected
    if isinstance(claim_value, list):
        return expected in claim_value
    return False


def decode_jwt(
    token: str,
    secret: Optional[bytes] = None,
    *,
    now: Optional[int] = None,
    leeway: Optional[int] = None,
    expected_issuer: Optional[str] = JWT_ISSUER,
    expected_audience: Optional[str] = JWT_AUDIENCE,
    expected_type: Optional[str] = TOKEN_TYPE_ACCESS,
    verify_expiry: bool = True,
) -> dict:
    """校验令牌并返回声明集合，任一层不通过抛对应的 TokenError 子类。

    expected_* 传 None 表示不校验该项，供签发侧自检或跨用途解析时放宽；
    verify_expiry=False 只跳过时效判定，签名与声明仍然校验，用于排查刚过期的令牌。
    now 显式传入时按该时间戳判定时效，便于测试与回溯。
    leeway 不传时取当前配置的时钟偏差，显式给值则按给的算。
    """
    tolerance = jwt_clock_leeway() if leeway is None else leeway
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
        raise ValueError("leeway 必须是非负整数")

    header_segment, payload_segment, signature_segment = _split_token(token)
    header = _decode_segment(header_segment, "头部")
    _check_algorithm(header)

    expected_signature = _sign(
        f"{header_segment}.{payload_segment}",
        get_jwt_secret() if secret is None else secret,
    )
    try:
        actual_signature = _b64url_decode(signature_segment)
    except (binascii.Error, ValueError):
        raise TokenMalformedError("令牌签名段无法解析") from None
    if not hmac.compare_digest(expected_signature, actual_signature):
        raise TokenSignatureError("令牌签名校验失败")

    claims = _decode_segment(payload_segment, "载荷")

    for name in JWT_REQUIRED_CLAIMS:
        _require_claim(claims, name)
    if expected_issuer is not None and claims["iss"] != expected_issuer:
        raise TokenClaimsError("令牌签发方不匹配", {"claim": "iss"})
    if expected_audience is not None and not _audience_matches(
        claims["aud"], expected_audience
    ):
        raise TokenClaimsError("令牌使用方不匹配", {"claim": "aud"})
    if expected_type is not None and claims["typ"] != expected_type:
        raise TokenClaimsError("令牌用途不匹配", {"claim": "typ"})

    issued_at = _timestamp_claim(claims, "iat")
    expires_at = _timestamp_claim(claims, "exp")
    if expires_at <= issued_at:
        raise TokenClaimsError("令牌有效期不成立", {"claim": "exp"})

    if verify_expiry:
        current = int(time.time()) if now is None else int(now)
        if current - tolerance >= expires_at:
            raise TokenExpiredError("令牌已过期", {"expired_at": expires_at})
        not_before = _timestamp_claim(claims, "nbf") if "nbf" in claims else issued_at
        if current + tolerance < max(not_before, issued_at):
            raise TokenNotYetValidError("令牌尚未生效", {"not_before": not_before})

    return claims


def verify_access_token(token: str, secret: Optional[bytes] = None, **kwargs) -> dict:
    """按 access token 的约定校验令牌，返回声明集合。

    用途固定为 access：refresh token 或其它用途的令牌走到这里会因 typ 不符被拒，
    避免两类令牌互相顶替。
    """
    kwargs.setdefault("expected_type", TOKEN_TYPE_ACCESS)
    return decode_jwt(token, secret, **kwargs)


def extract_bearer_token(authorization: Optional[str]) -> str:
    """从 Authorization 头取出 Bearer 令牌，缺失或格式不对时抛对应异常。"""
    if not isinstance(authorization, str) or not authorization.strip():
        raise TokenMissingError("未提供 Authorization 请求头")
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise TokenMalformedError("Authorization 头需为 Bearer <token> 形式")
    token = parts[1].strip()
    if not token:
        raise TokenMissingError("Bearer 后未携带令牌")
    return token


# ---- 请求身份解析：以签名令牌为准，旧会话令牌只作过渡 ----
#
# 接口层只调用 resolve_authorization，不必关心令牌是哪种形态。
# 身份一律回库取：令牌自带的 username / role 只说明签发时刻的状态，
# 改名、调角色之后下一次请求就该生效，权限判定不能建立在令牌快照上。


def find_by_id(user_id: Optional[str]) -> Optional[dict]:
    """按用户 id 查询可公开的用户信息，不存在返回 None。"""
    if not user_id:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        return _account(user) if user else None
    finally:
        db.close()


def _is_signed_token(token: str) -> bool:
    """是否为三段式签名令牌；旧版会话令牌是单段随机串，不含分隔点。"""
    return token.count(".") == 2


def _user_from_claims(claims: dict) -> Optional[dict]:
    """按令牌声明的 sub 回库取用户，账号已不存在时返回 None。"""
    return find_by_id(claims.get("sub"))


def resolve_token(token: str) -> Optional[dict]:
    """解析令牌对应的当前用户，无法确认身份时返回 None。

    签名令牌先过 verify_access_token（结构、算法、签名、时效、声明），
    任一层不通过都直接判为未登录，不再回落会话表：伪造或过期的令牌
    没有第二条通道可走。单段令牌按旧版会话令牌查会话表，只服务尚未
    换发新令牌的客户端，待令牌全面换发后可整段移除。
    """
    if not isinstance(token, str) or not token.strip():
        return None
    value = token.strip()
    if _is_signed_token(value):
        try:
            claims = verify_access_token(value)
        except TokenError:
            return None
        return _user_from_claims(claims)

    session = get_session(value)
    if not session:
        return None
    return _user_from_claims({"sub": session.get("user_id")}) or find_by_username(
        session.get("username")
    )


def resolve_authorization(authorization: Optional[str]) -> Optional[dict]:
    """从 Authorization 头解析当前用户，未携带、格式不对或校验失败返回 None。

    标准写法是 Bearer <token>；只给令牌原文也接受，兼容手工调试与旧客户端。
    失败一律返回 None 而不抛异常，由接口层按自己的语义处理——
    工单接口据此报未登录，页面与对话接口则退化成匿名访问。
    """
    try:
        token = extract_bearer_token(authorization)
    except TokenMissingError:
        return None
    except TokenMalformedError:
        token = (authorization or "").strip()
    return resolve_token(token)


def _record_session(user: dict, token: str, expires_at: int) -> None:
    """把已签发的令牌登记为一条会话记录。

    令牌本身自包含身份与有效期，这里登记是为了让仍按令牌查会话的路径
    （对话链路）继续可用：登录一次，两条解析路径都能拿到同一份登录态。
    记录的到期时间取自令牌的 exp，两条路径不会各自算出一套过期时间。
    """
    db = SessionLocal()
    try:
        db.add(
            Session(
                token=token,
                user_id=user["id"],
                username=user["username"],
                name=user.get("name") or "",
                role=user.get("role") or "customer",
                expires_at=expires_at,
            )
        )
        db.commit()
    finally:
        db.close()


def issue_login_token(user: dict, ttl: Optional[int] = None) -> dict:
    """为登录成功的用户签发 access token 并登记会话。

    返回令牌与其有效期元信息，供接口层原样回给客户端：
    expires_in 为相对秒数（客户端用来算刷新时机），expires_at 为绝对时间戳。
    ttl 不传时取当前配置的有效期，会话记录的到期时间与令牌保持一致。
    """
    payload = build_access_payload(user, ttl=ttl)
    token = sign_jwt(payload)
    _record_session(user, token, payload["exp"])
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": payload["exp"] - payload["iat"],
        "expires_at": payload["exp"],
    }


def get_session(token: str) -> Optional[dict]:
    """按 token 取会话，过期的会话顺手删除，无效或过期返回 None。"""
    if not token:
        return None
    db = SessionLocal()
    try:
        s = db.query(Session).filter(Session.token == token).first()
        if not s:
            return None
        if s.expired:
            db.delete(s)
            db.commit()
            return None
        return s.to_dict()
    finally:
        db.close()


def public_user(user: dict) -> dict:
    """从用户信息中挑出可公开字段。"""
    return {field: user.get(field) for field in _PUBLIC_FIELDS}
