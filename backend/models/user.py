import hashlib
import hmac
import time
from enum import Enum
from typing import Optional

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base, DB_TIMESTAMP, DB_TOKEN
from backend.services.password import (
    hash_password,
    is_bcrypt_hash,
    needs_rehash,
    verify_password,
)

# bcrypt 哈希固定为 60 字符；MySQL 列需兼容 64 字符的旧 SHA-256 十六进制摘要。
PASSWORD_HASH_LENGTH = 60
_MYSQL_PASSWORD_HASH_COLUMN_LENGTH = 64


class UserRole(str, Enum):
    """账号角色。

    只有两种：管理员可以查看与处理全量数据，客户只能访问本人的数据。
    角色以字符串落库，枚举供代码内比较与入参收敛使用，
    避免各处手写 "admin" 字面量、写错一个字母就变成另一种身份。
    """

    ADMIN = "admin"
    CUSTOMER = "customer"


# 合法角色取值集合：注册、改角色等入口据此校验，白名单之外的取值不进库
ROLE_VALUES = frozenset(role.value for role in UserRole)

# 未显式指定角色时的默认值：公开注册一律建成客户账号，取权限最小的那个
DEFAULT_ROLE = UserRole.CUSTOMER.value


def normalize_role(role, default: str = DEFAULT_ROLE) -> str:
    """把角色入参收敛为合法取值，无法识别时返回 default。

    权限判定只认白名单内的取值：空值、None、历史脏数据都落到 default；
    大小写与首尾空白先做归一，Admin 与 admin 视为同一角色，
    不会因为写法不同而意外获得或失去权限。
    """
    if isinstance(role, UserRole):
        return role.value
    if isinstance(role, str):
        value = role.strip().lower()
        if value in ROLE_VALUES:
            return value
    return default


def _digest_legacy(password: str) -> str:
    """迁移前的旧存法：不加盐的 sha256 摘要，仅供兼容读取，新写入不再产生。"""
    return hashlib.sha256(password.encode()).hexdigest()


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(
        String(PASSWORD_HASH_LENGTH).with_variant(String(_MYSQL_PASSWORD_HASH_COLUMN_LENGTH), "mysql"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(255), nullable=False)  # admin / customer，取值见 UserRole

    # ---- 角色读取：只做判定，不改变账号状态 ----

    @property
    def role_value(self) -> str:
        """归一化后的角色取值，无法识别时按客户角色处理。"""
        return normalize_role(self.role)

    @property
    def is_admin(self) -> bool:
        """是否管理员。"""
        return self.role_value == UserRole.ADMIN.value

    @property
    def is_customer(self) -> bool:
        """是否客户账号。"""
        return self.role_value == UserRole.CUSTOMER.value

    # ---- 密码读写：字段只存哈希，明文不落库、不进日志 ----

    def set_password(self, password: str) -> str:
        """把明文密码哈希后写入字段，返回实际落库的哈希串。

        密码为空、非字符串或超长时由哈希工具直接拒绝，调用方需处理异常。
        """
        self.password_hash = hash_password(password)
        return self.password_hash

    def check_password(self, password: str) -> bool:
        """校验明文密码是否与字段匹配。

        字段为 bcrypt 哈希时走算法比对；仍是旧版摘要时退化为摘要相等比较，
        保证字段迁移过程中存量用户仍能登录。空密码、非法密码一律不通过。
        """
        stored = self.password_hash or ""
        if not stored:
            return False
        if is_bcrypt_hash(stored):
            return verify_password(password, stored)
        if not password:
            return False
        try:
            candidate = _digest_legacy(password)
        except (UnicodeError, AttributeError):
            return False
        # 摘要比较用定长比较，避免按字符提前返回
        return hmac.compare_digest(candidate, stored)

    @property
    def is_bcrypt_password(self) -> bool:
        """字段是否已是 bcrypt 哈希。"""
        return is_bcrypt_hash(self.password_hash)

    @property
    def password_needs_upgrade(self) -> bool:
        """字段是否需要按当前算法与成本重写：旧版摘要或成本因子偏低时为真。"""
        return needs_rehash(self.password_hash)

    def upgrade_password(self, password: str) -> bool:
        """密码校验通过后把字段原地重写为 bcrypt 哈希。

        校验不通过、或字段已是同等强度的 bcrypt 哈希时不做改动，返回 False。
        """
        if not self.check_password(password):
            return False
        if self.is_bcrypt_password and not self.password_needs_upgrade:
            return False
        self.set_password(password)
        return True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "password_hash": self.password_hash,
            "name": self.name,
            "role": self.role,
        }


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(DB_TOKEN, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(255), nullable=False)
    # expires_at 是过期清理的扫描条件，建索引避免清一批过期会话也要全表扫
    expires_at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "name": self.name,
            "role": self.role,
            "expires_at": self.expires_at,
        }

    @property
    def expired(self) -> bool:
        return self.expires_at < time.time()


class RevokedToken(Base):
    """已作废令牌的登记记录。

    访问令牌自包含身份与有效期，不依赖会话表，签发后到过期前一直可用；
    登出要让它在剩余有效期内立刻失效，只能留一份「已作废」的凭据。
    记录按令牌的 jti 索引：jti 每次签发唯一、长度固定，比存令牌原文省空间，
    也不会随令牌变长而膨胀。记录只需保留到令牌自身到期，
    过期之后令牌本来就会被时效校验拒掉，早期记录可以整批清掉。
    """

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    token_type: Mapped[str] = mapped_column(String(255), nullable=False)  # access / refresh
    # expires_at 是过期清理的扫描条件，建索引避免整表扫描
    expires_at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False, index=True)
    revoked_at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    @property
    def expired(self) -> bool:
        """令牌本身是否已过期：过期后这条记录不再有拦截作用。"""
        return self.expires_at < time.time()

    def to_dict(self) -> dict:
        return {
            "jti": self.jti,
            "user_id": self.user_id,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "reason": self.reason,
        }


class RefreshToken(Base):
    """refresh token 登记记录：长期令牌的每一次签发都落一行，支撑按条吊销。

    令牌本身是自包含的签名 JWT（typ=refresh，有效期按天计），
    记录存在的意义是让吊销生效——revoke 之后即使令牌未过期也不再可用；
    过期记录可以整行删除，不影响其它未过期令牌。
    """

    __tablename__ = "refresh_tokens"

    token: Mapped[str] = mapped_column(DB_TOKEN, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # expires_at 是过期清理的扫描条件，建索引避免整表扫描
    expires_at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False, index=True)
    revoked_at: Mapped[Optional[float]] = mapped_column(DB_TIMESTAMP, nullable=True)

    @property
    def expired(self) -> bool:
        """是否已过期。"""
        return self.expires_at < time.time()

    @property
    def revoked(self) -> bool:
        """是否已被吊销。"""
        return self.revoked_at is not None

    @property
    def active(self) -> bool:
        """是否仍可使用：未吊销且未过期。"""
        return not self.revoked and not self.expired

    def revoke(self, when: Optional[float] = None) -> None:
        """标记吊销；重复吊销保持第一次的时间不被覆盖。"""
        if self.revoked_at is None:
            self.revoked_at = time.time() if when is None else float(when)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
        }
