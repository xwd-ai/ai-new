import time

from sqlalchemy import Float, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base

# SQLAlchemy 数据模型统一在此定义。
# 各模型按阶段迁移，逐步从 JSON 文件存储切到数据库。


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # admin / customer

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


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # basic / pro
    name: Mapped[str] = mapped_column(String, nullable=False)
    features: Mapped[list] = mapped_column(JSON, nullable=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "features": self.features}


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    product_tier: Mapped[str] = mapped_column(String, nullable=False)  # basic / pro
    status: Mapped[str] = mapped_column(String, nullable=False)  # active 等
    started_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "product_tier": self.product_tier,
            "status": self.status,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
        }
