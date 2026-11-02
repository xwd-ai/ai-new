import hashlib
import hmac
import time

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base
from backend.services.password import (
    hash_password,
    is_bcrypt_hash,
    needs_rehash,
    verify_password,
)

# password_hash 字段存 bcrypt 哈希串，固定 60 字符：$2b$ + 两位成本 + 53 位盐与摘要
PASSWORD_HASH_LENGTH = 60


def _digest_legacy(password: str) -> str:
    """迁移前的旧存法：不加盐的 sha256 摘要，仅供兼容读取，新写入不再产生。"""
    return hashlib.sha256(password.encode()).hexdigest()


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(PASSWORD_HASH_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # admin / customer

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

    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)

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
