from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class OrderStatus(str, Enum):
    """订单状态枚举。

    状态含义：
    - active：有效（订单正常，服务按服务期提供）
    - expired：过期（服务期结束且未续费，业务侧归档时写入）
    - cancelled：退订（用户主动退订，不再提供服务）

    订单状态是业务侧维护的生命周期标记，与服务期状态（按起止时间推导的
    服务中 / 未开始 / 已到期 / 无法判定）各自独立：状态有效但服务期未开始
    或已到期的订单都存在，两者要分别判断，不能互相推导。
    """

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# 状态中文名，供展示与话术直接使用
ORDER_STATUS_LABELS = {
    OrderStatus.ACTIVE: "有效",
    OrderStatus.EXPIRED: "过期",
    OrderStatus.CANCELLED: "退订",
}

# 合法状态值集合：入参校验只需判断成员关系，避免每次调用重新构造集合
VALID_STATUS_VALUES: frozenset[str] = frozenset(status.value for status in OrderStatus)


def as_status(value) -> Optional[OrderStatus]:
    """把入参解析为订单状态枚举：已是枚举直接返回，合法字符串转枚举，其余返回 None。

    未知取值不猜也不落默认状态——历史脏数据或业务侧新写入的状态，
    由调用方决定降级口径，不能默认当成有效放行。
    """
    if isinstance(value, OrderStatus):
        return value
    if isinstance(value, str) and value in VALID_STATUS_VALUES:
        return OrderStatus(value)
    return None


def is_valid_status(value: str) -> bool:
    """判断字符串是否为合法订单状态值。"""
    return value in VALID_STATUS_VALUES


def status_label(value) -> str:
    """订单状态显示名，未知状态返回「未知状态」。"""
    status = as_status(value)
    return ORDER_STATUS_LABELS[status] if status is not None else "未知状态"


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
    status: Mapped[str] = mapped_column(String, nullable=False)  # 取值见 OrderStatus
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
