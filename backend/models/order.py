from sqlalchemy import JSON, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


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
