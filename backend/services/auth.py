"""认证服务：账号、令牌与权限的唯一收口。

文件按职责纵向分层，上层只调用下层，同层之间不互相依赖：

一、错误与错误码
   AuthError 派生出一族业务异常，各自带稳定的 error_code 与建议的 HTTP 状态；
   接口层只负责把它们转成 {error, error_code, error_detail?}，不判断具体类型。

二、账号与密码
   用户从 users 表读取，对外只暴露 id / username / name / role，密码哈希不出本层；
   注册校验用户名格式与密码强度，通过后写 bcrypt 哈希，明文不落库；
   登录校验委托 authenticate，失败一律返回 None，调用方无从区分失败原因；
   校验通过且字段仍是旧版摘要或成本偏低时，趁这次登录原地升级，存量账号无需重置密码。

三、令牌原语
   紧凑格式（header.payload.signature）的编解码、HMAC-SHA256 签名与签名比对；
   有效期、时钟偏差与密钥都来自环境变量，调用时解析而非 import 时定死，
   配置缺失或取值非法时告警并回退默认值，不会因为一处配置写错就让服务起不来。

四、签发层
   按用途构造载荷（access / refresh 共用一套声明结构，仅 typ 与有效期不同），
   签名后登记：access 记入 sessions 表供按令牌查会话的路径使用，
   refresh 记入 refresh_tokens 表供按条吊销；返回值统一带 expires_in / expires_at。

五、校验层
   按「结构 → 算法 → 签名 → 声明 → 时效」逐级收口，任何一层不通过都抛 TokenError
   派生异常，错误码稳定可供客户端分支；校验本身不查库、不依赖会话表。
   请求身份解析（resolve_authorization）在这之上：令牌通过后按 sub 回库取用户，
   身份始终以库为准，改名、调角色下一次请求即生效，不必等令牌过期。

六、刷新层
   换发只认 typ=refresh 且登记表里未吊销、未过期的凭证，签名合格只是前提；
   换发只重签访问令牌，不延长续期凭证自身的寿命，续期次数不叠加寿命。

七、注销与清理
   登出把当前令牌的 jti 记入 revoked_tokens 黑名单并删除会话记录，
   此后该令牌即使未过期也判为未登录；过期凭据由 purge_expired_sessions 批量收敛。

八、权限层
   角色判定与拦截只此一处：require_login / require_role / require_admin 对应三档要求，
   scope_user_id 给出列表类查询的可见范围，接口层只声明需要什么身份。

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

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.database import SessionLocal
from backend.models import Session, User
from backend.models.user import DEFAULT_ROLE, RefreshToken, RevokedToken, UserRole, normalize_role
from backend.services import cache
from backend.services.password import MAX_PASSWORD_BYTES, PasswordHashError, verify_password

logger = logging.getLogger(__name__)


# ============================ 一、错误与错误码 ============================
#
# 具体原因只回给客户端一个稳定的错误码与简短说明，不暴露密钥、签名原文
# 或内部分支，避免把校验细节变成试探令牌构造的线索。


class AuthError(ValueError):
    """认证业务异常基类，携带错误码与建议的 HTTP 状态。

    对外暴露 code / message / details / http_status 四项，与订单、工单模块的
    异常同名同义，接口层据此用同一套逻辑组装错误响应；error_code 与 detail
    是既有名字，保留为同一份数据的别名，调用方与既有用例无需改动。
    """

    error_code = "AUTH_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    @property
    def code(self) -> str:
        """对外错误码，与工单/订单异常的 code 同名。"""
        return self.error_code

    @property
    def details(self) -> dict:
        """补充上下文，与工单/订单异常的 details 同名。"""
        return dict(self.detail or {})

    def to_payload(self) -> dict:
        payload = {"error": self.message, "error_code": self.error_code}
        if self.detail:
            payload["error_detail"] = self.detail
        return payload


# ---- 账号与密码：注册阶段的入参问题 ----


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


class PermissionDeniedError(AuthError):
    """已登录但身份不具备该操作所需的权限。"""

    error_code = "AUTH_FORBIDDEN"
    http_status = 403


# ---- 令牌校验：统一 401，按 error_code 区分失败环节 ----


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


# ---- 续期失败：签名合格但不能再换发的几种情形 ----
#
# 与令牌校验异常同属 401：客户端拿到这些错误码都该回到登录页，
# 区别只在于「这条续期凭证为什么不作数」，便于前端提示与日志定位。


class RefreshTokenError(TokenError):
    """续期失败基类。"""

    error_code = "AUTH_REFRESH_INVALID"


class RefreshTokenUnknownError(RefreshTokenError):
    """续期令牌不在登记表内：来路不明，或签发它的环境已被清库。"""

    error_code = "AUTH_REFRESH_UNKNOWN"


class RefreshTokenRevokedError(RefreshTokenError):
    """续期令牌已被吊销（登出或异常处理时失效）。"""

    error_code = "AUTH_REFRESH_REVOKED"


class RefreshTokenOwnerError(RefreshTokenError):
    """续期令牌指向的账号已不存在。"""

    error_code = "AUTH_REFRESH_USER_GONE"


# ============================ 二、账号与密码 ============================

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


def _account(user: User) -> dict:
    """把用户记录转成对外字典。"""
    return {field: getattr(user, field) for field in _PUBLIC_FIELDS}


def _username_exists(db, username: str) -> bool:
    """用户名是否已被占用，比较时忽略大小写（Admin 与 admin 视为同名）。"""
    return (
        db.query(User).filter(func.lower(User.username) == username.lower()).first()
        is not None
    )


def _query_user(db, username: str) -> Optional[User]:
    """按用户名取用户记录，未命中返回 None。"""
    return db.query(User).filter(User.username == username).first()


def register_user(
    username: str, password: str, name: str = "", role: str = DEFAULT_ROLE
) -> dict:
    """创建账号并返回可公开的用户信息。

    校验顺序：用户名格式 → 密码强度 → 用户名占用，全部通过才写入。
    密码经 User.set_password 哈希后落库，明文不进入返回值与日志。
    role 由调用方决定，公开注册入口固定传 customer，不开放管理员身份的自助创建；
    入参经归一化，写错或留空都落到权限最小的客户角色。
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
            role=normalize_role(role),
        )
        user.set_password(password)
        if db.get_bind().dialect.name == "sqlite":
            # 与 Go 共用 SQLite 写锁，防止仅大小写不同的用户名并发注册。
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
            if _username_exists(db, account_name):
                raise UsernameTakenError("用户名已被占用")
        db.add(user)
        db.commit()
        return _account(user)
    except IntegrityError:
        # 预检查与写入之间仍可能被其它请求抢先插入，唯一约束兜住并统一按「已占用」返回
        db.rollback()
        raise UsernameTakenError("用户名已被占用") from None
    finally:
        db.close()


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


def find_by_id(user_id: Optional[str]) -> Optional[dict]:
    """按用户 id 查询可公开的用户信息，不存在返回 None。

    令牌只携带 sub，请求身份一律经这里回库确认：用户被删除后旧令牌即失效，
    不会因为令牌仍在有效期内而认出一个已不存在的账号。
    """
    if not user_id:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
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


# ============================ 三、令牌原语 ============================
#
# 签名算法固定 HS256，只依赖标准库的 HMAC-SHA256 与 base64url，不额外引入运行时依赖。
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
TOKEN_TYPE_REFRESH = "refresh"

# 有效期允许区间：太短会让客户端频繁换发，太长则放大令牌泄露后的影响面
JWT_TTL_MIN = 60  # 1 分钟
JWT_TTL_MAX = 7 * 24 * 3600  # 7 天

# refresh token 有效期单独配置：默认按天计的长期凭证，区间比 access 宽，
# 但也设上限，不让一次配置失误把续期凭证放到不可控的年限
JWT_REFRESH_TTL = 30 * 24 * 3600  # refresh token 默认有效期：30 天
JWT_REFRESH_TTL_ENV = "AI_NEW_JWT_REFRESH_TTL"
JWT_REFRESH_TTL_MIN = 3600  # 1 小时
JWT_REFRESH_TTL_MAX = 180 * 24 * 3600  # 180 天

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


def jwt_refresh_ttl() -> int:
    """当前生效的 refresh token 有效期（秒）。"""
    return _env_seconds(
        JWT_REFRESH_TTL_ENV, JWT_REFRESH_TTL, JWT_REFRESH_TTL_MIN, JWT_REFRESH_TTL_MAX
    )


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


def _sign(segments: str, secret: bytes) -> bytes:
    """对 header.payload 段做 HMAC-SHA256 签名。"""
    return hmac.new(secret, segments.encode("ascii"), hashlib.sha256).digest()


def sign_jwt(payload: dict, secret: Optional[bytes] = None) -> str:
    """把声明集合签成紧凑格式令牌，返回 header.payload.signature 三段字符串。

    payload 只读，函数不修改入参；secret 缺省用模块级配置的密钥。
    """
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    segments = f"{_b64url_encode(_compact_json(header))}.{_b64url_encode(_compact_json(payload))}"
    signature = _sign(segments, get_jwt_secret() if secret is None else secret)
    return f"{segments}.{_b64url_encode(signature)}"


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
        "refresh_ttl": jwt_refresh_ttl(),
        "clock_leeway": jwt_clock_leeway(),
        "secret_source": JWT_SECRET_ENV if _env_text(JWT_SECRET_ENV) else "dev-default",
        "secret_length_ok": len(get_jwt_secret()) >= JWT_SECRET_MIN_BYTES,
    }


# ============================ 四、签发层 ============================
#
# 载荷只放鉴权必需的声明：身份信息靠 sub 回库取，令牌不承担用户快照的职责。
# 两类令牌共用同一套声明结构与同一个签名器，仅 typ 与有效期不同，
# 因此构造逻辑收在一处，避免两边声明字段写歪导致互相顶替。


def _build_payload(
    user: dict,
    token_type: str,
    default_ttl,
    ttl: Optional[int],
    issued_at: Optional[int],
) -> dict:
    """按给定用途构造载荷，access 与 refresh 共用。

    声明分工：iss/aud 限定签发方与使用方；sub 为用户 id，是回查用户的唯一依据；
    username/role 供鉴权与日志直接取用，不做权限判定以外的业务用途；
    iat/nbf/exp 为秒级时间戳，nbf 与 iat 取同一时刻；jti 是一次性标识，
    为吊销名单提供锚点；typ 区分用途，两类令牌不能互相顶替。

    ttl 不传时按 default_ttl 解析当前配置，显式给值则按给的算。
    """
    if not isinstance(user, dict) or not user.get("id") or not user.get("username"):
        raise TokenSigningError("用户信息缺少签发令牌所需的字段")

    lifetime = default_ttl() if ttl is None else ttl
    if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
        raise TokenSigningError("令牌有效期必须为正整数")

    now = int(time.time()) if issued_at is None else int(issued_at)
    return {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": user["id"],
        "username": user["username"],
        "role": normalize_role(user.get("role")),
        "typ": token_type,
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
        "jti": uuid.uuid4().hex,
    }


def build_access_payload(
    user: dict,
    ttl: Optional[int] = None,
    issued_at: Optional[int] = None,
) -> dict:
    """构造 access token 载荷：默认取短期有效期配置，typ 固定 access。"""
    return _build_payload(user, TOKEN_TYPE_ACCESS, jwt_access_ttl, ttl, issued_at)


def build_refresh_payload(
    user: dict,
    ttl: Optional[int] = None,
    issued_at: Optional[int] = None,
) -> dict:
    """构造 refresh token 载荷：默认取长期有效期配置，typ 固定 refresh。

    username / role 只作日志与展示参考，刷新换发时一律按 sub 回库取用户，
    不依赖载荷里的快照。
    """
    return _build_payload(user, TOKEN_TYPE_REFRESH, jwt_refresh_ttl, ttl, issued_at)


def create_access_token(user: dict, ttl: Optional[int] = None) -> str:
    """为已通过校验的用户签发 access token。"""
    return sign_jwt(build_access_payload(user, ttl=ttl))


def create_refresh_token(user: dict, ttl: Optional[int] = None) -> str:
    """为已通过校验的用户签发 refresh token（只签名，不落库）。"""
    return sign_jwt(build_refresh_payload(user, ttl=ttl))


def _token_meta(payload: dict, token: str) -> dict:
    """把已签发的令牌与其有效期元信息组装成对外的统一形态。

    expires_in 是相对秒数（客户端用来算刷新时机），expires_at 是绝对时间戳；
    两类令牌用同一组字段名，客户端不必为续期写第二套解析逻辑。
    """
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": payload["exp"] - payload["iat"],
        "expires_at": payload["exp"],
    }


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
                role=normalize_role(user.get("role")),
                expires_at=expires_at,
            )
        )
        db.commit()
    finally:
        db.close()


def _record_refresh_token(user_id: str, token: str, expires_at: int) -> None:
    """把已签发的 refresh token 登记入库，供后续吊销与清理。"""
    db = SessionLocal()
    try:
        db.add(RefreshToken(token=token, user_id=user_id, expires_at=expires_at))
        db.commit()
    finally:
        db.close()


def issue_refresh_token(user: dict, ttl: Optional[int] = None) -> dict:
    """为登录成功的用户签发 refresh token 并登记吊销记录。

    返回形态与 access 签发一致：令牌原文、类型与有效期元信息。
    ttl 不传时取长期默认配置；登记记录的到期时间与令牌 exp 保持一致。
    """
    payload = build_refresh_payload(user, ttl=ttl)
    token = sign_jwt(payload)
    _record_refresh_token(payload["sub"], token, payload["exp"])
    return _token_meta(payload, token)


def issue_login_token(user: dict, ttl: Optional[int] = None) -> dict:
    """为登录成功的用户签发 access token 与 refresh token，并登记会话。

    返回两类令牌及其有效期元信息，供接口层原样回给客户端；
    refresh_* 三段是长期续期凭证，落库支持按条吊销。
    ttl 只作用于 access token，refresh token 有效期独立取长期配置；
    会话记录的到期时间与 access token 保持一致。
    """
    payload = build_access_payload(user, ttl=ttl)
    token = sign_jwt(payload)
    _record_session(user, token, payload["exp"])
    refresh = issue_refresh_token(user)
    return {
        **_token_meta(payload, token),
        "refresh_token": refresh["token"],
        "refresh_expires_in": refresh["expires_in"],
        "refresh_expires_at": refresh["expires_at"],
    }


# ============================ 五、校验层 ============================
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


def verify_refresh_token(token: str, secret: Optional[bytes] = None, **kwargs) -> dict:
    """按 refresh token 的约定校验令牌，返回声明集合。

    用途固定为 refresh：access token 走到这里会因 typ 不符被拒。
    换发只认这一类令牌，避免短期凭证被当成长期凭证无限续期。
    """
    kwargs.setdefault("expected_type", TOKEN_TYPE_REFRESH)
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
    没有第二条通道可走。通过后再查一次黑名单，登出过的令牌在这一步被拦下——
    签名仍合格、也未到期，但已经不该再代表任何人。单段令牌按旧版会话令牌
    查会话表，只服务尚未换发新令牌的客户端，待令牌全面换发后可整段移除。
    """
    if not isinstance(token, str) or not token.strip():
        return None
    value = token.strip()
    if _is_signed_token(value):
        try:
            claims = verify_access_token(value)
        except TokenError:
            return None
        if is_token_revoked(claims.get("jti")):
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


# ============================ 六、刷新层 ============================
#
# 换发只认 typ=refresh 的令牌，且必须能在登记表里找到一条未吊销、未过期的记录：
# 签名合格只说明令牌由本服务签发，能换发则还需要「这条凭证仍然有效」。
# 换发不延长续期凭证本身的有效期，只重新签发访问令牌，
# 因此长期凭证的到期时间仍由首次登录决定，续期次数不叠加寿命。


def _refresh_record(db, token: str, user_id: str) -> RefreshToken:
    """取出可用的续期记录，未登记、已吊销、已过期或与令牌主体不符时抛异常。

    主体比对是防串用的兜底：登记表按令牌原文索引，正常情况下两者必然一致，
    一旦出现不一致说明记录被改写，宁可判为无效也不放行。
    """
    record = db.query(RefreshToken).filter(RefreshToken.token == token).first()
    if record is None:
        raise RefreshTokenUnknownError("续期令牌未登记，请重新登录")
    if record.user_id != user_id:
        raise RefreshTokenUnknownError("续期令牌与登记信息不一致，请重新登录")
    if record.revoked:
        raise RefreshTokenRevokedError("续期令牌已失效，请重新登录")
    if record.expired:
        raise TokenExpiredError("续期令牌已过期", {"expired_at": record.expires_at})
    return record


def revoke_refresh_token(token: str) -> bool:
    """吊销一条 refresh token：标记 revoked_at，未过期也不再可用。

    令牌原文不存在时返回 False；存在则标记吊销并返回 True。
    重复吊销保持首次时间，返回值仍为 True（已是吊销状态，语义幂等）。
    """
    if not isinstance(token, str) or not token.strip():
        return False
    db = SessionLocal()
    try:
        record = db.query(RefreshToken).filter(RefreshToken.token == token.strip()).first()
        if not record:
            return False
        record.revoke()
        db.commit()
        return True
    finally:
        db.close()


def refresh_access_token(refresh_token: str, ttl: Optional[int] = None) -> dict:
    """用 refresh token 换发新的 access token，返回两类令牌的有效期元信息。

    校验顺序：签名与声明 → 登记记录 → 账号是否仍在。
    换发结果与原登录响应同构（token / expires_in / expires_at 等），
    客户端按同一套字段更新本地状态即可，不必为续期写第二套解析逻辑。
    续期凭证原样回传，不轮换：换发失败时客户端手里仍有可用凭证，
    不会因为一次网络异常把用户踢回登录页。
    """
    value = refresh_token.strip() if isinstance(refresh_token, str) else ""
    if not value:
        raise TokenMissingError("缺少续期令牌")

    claims = verify_refresh_token(value)
    # 登出时续期令牌可能只进了黑名单而未留下吊销记录（例如登记行已被清理），
    # 两条判据都看一遍，任一条命中都不再换发
    if is_token_revoked(claims.get("jti")):
        raise RefreshTokenRevokedError("续期令牌已失效，请重新登录")

    db = SessionLocal()
    try:
        _refresh_record(db, value, claims["sub"])
        user = db.query(User).filter(User.id == claims["sub"]).first()
        if user is None:
            raise RefreshTokenOwnerError("账号不存在，请重新登录")
        account = _account(user)
    finally:
        db.close()

    payload = build_access_payload(account, ttl=ttl)
    token = sign_jwt(payload)
    # 新令牌同样登记为会话：对话链路按令牌查会话，不登记会立刻失去登录态
    _record_session(account, token, payload["exp"])

    return {
        **_token_meta(payload, token),
        "refresh_token": value,
        "refresh_expires_in": max(claims["exp"] - int(time.time()), 0),
        "refresh_expires_at": claims["exp"],
        "user": account,
    }


# ============================ 七、注销与清理 ============================
#
# 令牌是自包含的，服务端不持有它的状态，登出只能反向登记一份「作废名单」：
# 校验时先看签名与时效，再查 jti 是否在名单里，命中即视为未登录。
# 名单按 jti 索引而不是按令牌原文：jti 定长且唯一，存原文既占空间，
# 也让「同一用户多端登录」的记录难以区分。
# 名单只保留到令牌自身到期，过期后令牌本就会被时效校验拦下，记录可以清掉。

REVOKE_REASON_LOGOUT = "logout"


def _blacklist_token(
    jti: Optional[str],
    user_id: str,
    token_type: str,
    expires_at: int,
    reason: str = REVOKE_REASON_LOGOUT,
) -> bool:
    """把一条令牌记入作废名单，返回本次是否新增记录。

    已有记录不覆盖：重复登出保持首次作废时间，也避免并发登出把时间改来改去。
    无 jti 的旧令牌无法被单独标识，不登记（这类令牌只能等它自己过期）。
    """
    if not jti:
        return False
    db = SessionLocal()
    try:
        if db.query(RevokedToken).filter(RevokedToken.jti == jti).first() is not None:
            return False
        db.add(
            RevokedToken(
                jti=jti,
                user_id=user_id or "",
                token_type=token_type,
                expires_at=float(expires_at),
                revoked_at=time.time(),
                reason=reason,
            )
        )
        db.commit()
        # 吊销已落库，清掉缓存让下次校验回源（未启用缓存时为空操作）
        cache.delete(f"{_REVOKED_CACHE_PREFIX}{jti}")
        return True
    except IntegrityError:
        # 并发登出时另一条请求已写入同一 jti，按「已作废」处理
        db.rollback()
        return False
    finally:
        db.close()


# 吊销检查的缓存：前缀 + 两种 TTL
_REVOKED_CACHE_PREFIX = "auth:revoked:"
# 已吊销是终态，缓存 1 小时（过期回源仍会命中数据库记录）
_REVOKED_FOUND_TTL = 3600


def is_token_revoked(jti: Optional[str]) -> bool:
    """令牌是否已作废。

    只按 jti 判定，不看有效期：名单里的记录本身就是「这条令牌不该再用」的结论。
    缺少 jti 的令牌返回 False——它无从被登记，只能靠时效约束。

    只缓存已作废结果。Go 可以直接写入共享数据库，未作废结果必须回源，
    包括迁移前留在缓存里的旧结果。
    """
    if not jti:
        return False

    cache_key = f"{_REVOKED_CACHE_PREFIX}{jti}"
    cached = cache.get(cache_key)
    if cached == "1":
        return True

    db = SessionLocal()
    try:
        revoked = (
            db.query(RevokedToken).filter(RevokedToken.jti == jti).first() is not None
        )
    finally:
        db.close()

    if revoked:
        cache.set(cache_key, "1", ttl=_REVOKED_FOUND_TTL)
    return revoked


def _drop_session(token: str) -> bool:
    """删除令牌对应的会话记录，返回是否确有删除。

    对话链路仍按令牌原文查会话表，光把令牌拉黑不够：会话记录还在，
    对话接口就还能认出这个人。登出必须两头一起清。
    """
    if not isinstance(token, str) or not token.strip():
        return False
    db = SessionLocal()
    try:
        record = db.query(Session).filter(Session.token == token.strip()).first()
        if record is None:
            return False
        db.delete(record)
        db.commit()
        return True
    finally:
        db.close()


def _logout_claims(token: str) -> dict:
    """校验待登出的访问令牌，过期也接受。

    客户端手里的访问令牌刚过期就来登出是正常时序，不该被时效判定挡在门外，
    否则用户会遇到「想退出却退不掉」。签名、算法与声明的校验一步不减：
    放宽时效不等于接受伪造令牌，否则任何人都能往名单里塞记录。
    """
    try:
        return verify_access_token(token)
    except TokenExpiredError:
        return decode_jwt(token, verify_expiry=False)


def logout(
    access_token: str,
    refresh_token: str = "",
    reason: str = REVOKE_REASON_LOGOUT,
) -> dict:
    """登出：作废当前访问令牌，可选一并吊销续期令牌。

    顺序上先校验、后写入：续期令牌若有问题，在改动任何状态之前就拒绝，
    不留下「访问令牌已作废、续期令牌却没吊销」的半截结果。
    续期令牌可选，但给了就必须属于当前登录用户——否则等于允许拿别人的
    长期凭证替别人注销，这里直接拒绝而不是默默忽略。

    重复调用返回同样的成功语义：客户端不必区分首次登出与重复登出，
    返回值里的 revoked_* 只说明本次是否新增了作废记录。
    """
    value = access_token.strip() if isinstance(access_token, str) else ""
    if not value:
        raise TokenMissingError("缺少认证令牌")
    claims = _logout_claims(value)

    refresh_value = refresh_token.strip() if isinstance(refresh_token, str) else ""
    refresh_claims: Optional[dict] = None
    if refresh_value:
        refresh_claims = verify_refresh_token(refresh_value, verify_expiry=False)
        if refresh_claims.get("sub") != claims.get("sub"):
            raise TokenClaimsError(
                "续期令牌不属于当前登录用户", {"claim": "sub"}
            )

    revoked_access = _blacklist_token(
        claims.get("jti"), claims.get("sub", ""), TOKEN_TYPE_ACCESS, claims["exp"], reason
    )
    session_removed = _drop_session(value)

    revoked_refresh = False
    if refresh_claims is not None:
        # revoked_refresh 与 revoked_access 同口径：只在本次新增作废记录时为真，
        # 重复登出两头都是 False。登记表的吊销标记照常幂等写入，不参与该口径。
        revoked_refresh = _blacklist_token(
            refresh_claims.get("jti"),
            refresh_claims.get("sub", ""),
            TOKEN_TYPE_REFRESH,
            refresh_claims["exp"],
            reason,
        )
        revoke_refresh_token(refresh_value)

    return {
        "message": "已退出登录",
        "user": find_by_id(claims.get("sub")) or {},
        "revoked_access": revoked_access,
        "access_expires_at": int(claims["exp"]),
        "session_removed": session_removed,
        "revoked_refresh": revoked_refresh,
        "refresh_expires_at": int(refresh_claims["exp"]) if refresh_claims else None,
    }


# ---- 过期凭据清理：让表规模跟随活跃登录数 ----
#
# 会话、续期登记、作废名单三张表都带到期时间，到期即失效，
# 但行本身不会自行消失：会话过期后仍躺在 sessions 里，
# 黑名单记录过了令牌有效期也不再拦截任何请求，续期凭证过期后同样只是占位。
# 清理按到期时间批量删除，默认不留缓冲（过期即可清），
# 需要回溯刚过期的登录时用 retention 留一段观察窗口。
# 单批删除量设上限：SQLite 一次写事务过大既占写锁也容易拖长响应，
# 积压较多时分多次调用即可，truncated 为真表示还有剩余。

SESSION_RETENTION = 0  # 过期后额外保留的秒数，默认不留
SESSION_RETENTION_MAX = 30 * 24 * 3600  # 保留窗口上限：30 天，避免误配成永久不删
PURGE_BATCH_LIMIT = 2000  # 单次单表删除上限
PURGE_BATCH_MAX = 10000


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    """把入参收敛到允许区间：非整数取默认值，越界取边界。

    清理是运维动作，参数写错不该让服务报错停下，按最保守的边界执行即可；
    真正的入参校验在接口层的 schema 上完成。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, min(value, maximum))


def purge_expired_sessions(
    retention: int = SESSION_RETENTION,
    limit: int = PURGE_BATCH_LIMIT,
    now: Optional[float] = None,
) -> dict:
    """删除三张表中已过期的记录，返回各表清理条数。

    - sessions：过期会话不再可用，get_session 命中时也会顺手删单条，
      这里做的是整批收敛，避免长期不登录的账号把表撑大；
    - refresh_tokens：过期的续期登记行留着只会让换发多走一次查询，
      过期即删不会改变换发结果（记录缺失与记录过期同样判为 401）；
    - revoked_tokens：作废名单只需要保留到令牌自身到期，
      到期后的令牌本就过不了时效校验，记录再无拦截作用。

    cutoff 之前的记录视为过期：retention>0 时把截止时间往前推，
    相当于多留一段刚过期的记录。now 显式传入便于回溯与测试。
    """
    keep = _bounded_int(retention, SESSION_RETENTION, 0, SESSION_RETENTION_MAX)
    batch = _bounded_int(limit, PURGE_BATCH_LIMIT, 1, PURGE_BATCH_MAX)
    current = time.time() if now is None else float(now)
    cutoff = current - keep

    deleted = {"sessions": 0, "refresh_tokens": 0, "revoked_tokens": 0}
    truncated = False

    db = SessionLocal()
    try:
        for name, model, key, column in (
            ("sessions", Session, Session.token, Session.expires_at),
            ("refresh_tokens", RefreshToken, RefreshToken.token, RefreshToken.expires_at),
            ("revoked_tokens", RevokedToken, RevokedToken.jti, RevokedToken.expires_at),
        ):
            # 时钟偏差窗口内，访问令牌仍可能通过签名校验，保留作废标记。
            expires_before = min(cutoff, current - jwt_clock_leeway()) if name == "revoked_tokens" else cutoff
            keys = [row[0] for row in db.query(key).filter(column < expires_before).limit(batch)]
            if keys:
                db.query(model).filter(key.in_(keys)).delete(synchronize_session=False)
            deleted[name] = len(keys)
            # 取满一批说明可能还有剩余，交给调用方决定是否继续清
            truncated = truncated or len(keys) >= batch
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    finally:
        db.close()

    total = sum(deleted.values())
    return {
        "message": f"已清理 {total} 条过期登录记录",
        "deleted": deleted,
        "total": total,
        "cutoff": int(cutoff),
        "retention": keep,
        "truncated": truncated,
    }


# ============================ 八、权限层 ============================
#
# 角色只有两种，判定与拦截都收在本段，是权限的唯一出口：
# 管理员可以读写全量数据，客户只能访问本人的数据。
# 接口层只声明「这一步需要什么身份」，不自己比对角色字符串，
# 少一处判断就少一处漏判，也不会因为把 "Admin" 当成管理员而放开越权。
#
# 判定一律基于回库取到的用户信息，不用令牌里的角色快照：
# 调过角色的人下一次请求就按新角色生效，不必等旧令牌过期。
# 未登录与无权限分开报：401 提示客户端去登录，403 说明已登录但不该做这件事。

ADMIN_ROLE = UserRole.ADMIN.value
CUSTOMER_ROLE = UserRole.CUSTOMER.value


def role_of(user: Optional[dict]) -> str:
    """取用户角色，未登录或取值无法识别时按客户角色处理。"""
    return normalize_role((user or {}).get("role"))


def is_admin(user: Optional[dict]) -> bool:
    """是否管理员。"""
    return role_of(user) == ADMIN_ROLE


def is_customer(user: Optional[dict]) -> bool:
    """是否客户账号；未登录不算客户，避免匿名请求被当作客户放行。"""
    return bool(user) and role_of(user) == CUSTOMER_ROLE


def has_role(user: Optional[dict], *roles: str) -> bool:
    """用户是否属于给定角色之一；未登录或角色列表为空时返回 False。

    角色入参同走归一化，枚举与字符串都可以传，写错的值会被丢掉而不是放宽判定。
    """
    if not user:
        return False
    allowed = {normalize_role(role, default="") for role in roles}
    allowed.discard("")
    return bool(allowed) and role_of(user) in allowed


def require_login(user: Optional[dict]) -> dict:
    """要求已登录，未登录抛 TokenMissingError（401）。"""
    if not user:
        raise TokenMissingError("未登录")
    return user


def require_role(user: Optional[dict], *roles: str, action: str = "") -> dict:
    """要求用户属于给定角色之一，返回用户信息。

    未登录 401，已登录但角色不符 403；错误详情给出所需角色与当前角色，
    前端据此区分「去登录」与「没有权限」，日志里也能看清是谁被拦。
    action 只进文案与详情，说明是哪一步被拦，不参与判定。
    """
    account = require_login(user)
    if has_role(account, *roles):
        return account

    required = [normalize_role(role, default="") for role in roles]
    required = [role for role in required if role] or [ADMIN_ROLE]
    detail = {"required_roles": required, "role": role_of(account)}
    if action:
        detail["action"] = action
        message = f"当前账号无权{action}"
    else:
        message = "当前账号无权执行该操作"
    raise PermissionDeniedError(message, detail)


def require_admin(user: Optional[dict], action: str = "") -> dict:
    """要求管理员身份：未登录 401，非管理员 403。"""
    return require_role(user, ADMIN_ROLE, action=action)


def scope_user_id(user: Optional[dict], include_admin_all: bool = True) -> Optional[str]:
    """列表类查询的数据范围：None 表示全量，否则限定为该用户号。

    管理员在 include_admin_all 为真时看全量，为假时与客户一样只看本人；
    客户无论参数如何都只看本人。分页列表、待确认列表、统计共用这一口径，
    避免每个查询接口各写一遍角色判断，也不会出现某个接口漏掉隔离。
    未登录直接拒绝：拿不到身份时宁可报错，也不返回一个范围不明的全量查询。
    """
    account = require_login(user)
    if is_admin(account) and include_admin_all:
        return None
    return account.get("id")


def public_user(user: dict) -> dict:
    """从用户信息中挑出可公开字段。"""
    return {field: user.get(field) for field in _PUBLIC_FIELDS}
