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


# 合法状态流转表：key 为当前状态，value 为允许流转到的目标状态集合。
# 未出现在映射中的流转一律视为非法，由状态校验统一拒绝。
#
# 业务口径：
# - 有效 → 过期：服务期结束且未续费，业务侧归档；
# - 有效 → 退订：用户主动退订，服务立即停止；
# - 过期 → 有效：续费成功，原订单恢复服务（订单号不变，续费记录另行保存）；
# - 过期 → 退订：过期后用户明确不再续费，归档为退订；
# - 退订是终态：退订后重新购买应生成新订单，复活旧订单会让续费历史与
#   服务期计算混在一起，也会让已退款的订单重新具备服务能力。
ORDER_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.ACTIVE: {OrderStatus.EXPIRED, OrderStatus.CANCELLED},
    OrderStatus.EXPIRED: {OrderStatus.ACTIVE, OrderStatus.CANCELLED},
    OrderStatus.CANCELLED: set(),
}

# 终态集合：进入后不再有任何允许的目标状态，供展示与二次校验复用
TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    status for status, targets in ORDER_TRANSITIONS.items() if not targets
)


class OrderErrorCode:
    """订单业务错误码。

    错误码是接口契约的一部分：文案可以调整，错误码保持不变，
    调用方按错误码分支处理，不必解析中文说明。
    """

    NOT_FOUND = "ORDER_NOT_FOUND"
    UNKNOWN_STATUS = "ORDER_UNKNOWN_STATUS"
    INVALID_TRANSITION = "ORDER_INVALID_TRANSITION"
    INVALID_FILTER = "ORDER_INVALID_FILTER"


class OrderBusinessError(ValueError):
    """订单业务异常基类，同时继承 ValueError 以兼容按入参错误捕获的调用方。

    http_status 与工单模块的异常保持同一套约定，接口层据此直接转成响应码：
    业务规则拒绝按 409，入参或取数问题按 400 / 404，避免所有失败都落成 500。
    """

    code = OrderErrorCode.INVALID_TRANSITION
    http_status = 409
    default_message = "订单状态操作失败"

    def __init__(self, message: Optional[str] = None, *, code: Optional[str] = None, details: Optional[dict] = None):
        self.code = code or type(self).code
        self.message = message or type(self).default_message
        self.details = dict(details or {})
        super().__init__(self.message)

    def to_payload(self) -> dict:
        """转为统一的错误响应体，补充上下文只在有值时出现。"""
        payload = {"error": self.message, "error_code": self.code}
        if self.details:
            payload["error_detail"] = self.details
        return payload


class OrderStatusTransitionError(OrderBusinessError):
    """订单状态非法流转。

    未知状态值与合法状态之间的非法流转分开给码：前者是入参或数据问题，
    后者是业务规则拒绝，调用方需要区别对待。
    """

    def __init__(self, current, target, message: Optional[str] = None, *, code: Optional[str] = None):
        self.current = "" if current is None else str(current)
        self.target = "" if target is None else str(target)
        super().__init__(
            message or f"非法状态流转：{self.current} -> {self.target}",
            code=code,
            details={"from_status": self.current, "to_status": self.target},
        )


class OrderNotFoundError(OrderBusinessError):
    """订单不存在或不属于当前用户。"""

    code = OrderErrorCode.NOT_FOUND
    http_status = 404
    default_message = "订单不存在"

    def __init__(self, order_id: Optional[str] = None, message: Optional[str] = None):
        details = {"order_id": order_id} if order_id else None
        super().__init__(message, details=details)


class OrderQueryError(OrderBusinessError):
    """订单查询入参不可用：未知状态、未知版本、非法分页或排序等。

    与状态流转错误分开给码：这类是查询条件的问题，调用方改了入参即可重试，
    不需要按业务规则判断能不能继续。
    """

    code = OrderErrorCode.INVALID_FILTER
    http_status = 400
    default_message = "订单查询条件不可用"


def can_transition(current, target) -> bool:
    """判断订单能否从 current 流转到 target，任一状态不可识别均为否。"""
    resolved_current = as_status(current)
    resolved_target = as_status(target)
    if resolved_current is None or resolved_target is None:
        return False
    return resolved_target in ORDER_TRANSITIONS[resolved_current]


def next_statuses(value) -> list[str]:
    """列出当前状态允许流转到的目标状态值，顺序与枚举声明一致。

    未知状态返回空列表：给不出可选项时宁可不给，也不要凭猜测列出一串
    实际上会被状态机拒绝的按钮。
    """
    current = as_status(value)
    if current is None:
        return []
    allowed = ORDER_TRANSITIONS[current]
    return [status.value for status in OrderStatus if status in allowed]


def is_terminal(value) -> bool:
    """判断状态是否为终态，未知状态按非终态处理（交由调用方另行核实）。"""
    current = as_status(value)
    return current in TERMINAL_STATUSES


def validate_transition(current, target, *, allow_same: bool = True) -> OrderStatus:
    """校验订单状态流转，合法时返回解析后的目标状态，非法时抛出异常。

    入参同时接受枚举与字符串。先判状态值是否可识别，再判流转是否在表内，
    让「空值 / 拼错的状态」与「业务上不允许的流转」得到不同的错误码。

    allow_same 控制「当前状态与目标状态相同」是否算通过：订单状态常由
    定时任务与人工操作并发写入，重复置为同一状态按幂等处理更稳妥，
    由调用方据此跳过写库，而不是报错让任务整体失败。
    """
    resolved_current = as_status(current)
    if resolved_current is None:
        raise OrderStatusTransitionError(
            current,
            target,
            f"未知的当前状态：{'' if current is None else current}",
            code=OrderErrorCode.UNKNOWN_STATUS,
        )

    resolved_target = as_status(target)
    if resolved_target is None:
        raise OrderStatusTransitionError(
            resolved_current.value,
            target,
            f"未知的目标状态：{'' if target is None else target}",
            code=OrderErrorCode.UNKNOWN_STATUS,
        )

    if resolved_target == resolved_current:
        if allow_same:
            return resolved_target
        raise OrderStatusTransitionError(
            resolved_current.value,
            resolved_target.value,
            f"订单已是{ORDER_STATUS_LABELS[resolved_target]}状态",
            code=OrderErrorCode.INVALID_TRANSITION,
        )

    if resolved_target not in ORDER_TRANSITIONS[resolved_current]:
        allowed = "、".join(ORDER_STATUS_LABELS[OrderStatus(value)] for value in next_statuses(resolved_current))
        hint = f"，当前状态只允许流转到{allowed}" if allowed else "，该状态已结束流转"
        raise OrderStatusTransitionError(
            resolved_current.value,
            resolved_target.value,
            f"订单不能从{ORDER_STATUS_LABELS[resolved_current]}变更为"
            f"{ORDER_STATUS_LABELS[resolved_target]}{hint}",
            code=OrderErrorCode.INVALID_TRANSITION,
        )

    return resolved_target


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # basic / pro
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    features: Mapped[list] = mapped_column(JSON, nullable=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "features": self.features}


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # user_id / status / expires_at 是用户订单查询、管理筛选与到期归档扫描的高频条件，
    # 各建单列索引让这些查询走索引定位而不是全表扫描
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    product_tier: Mapped[str] = mapped_column(String(255), nullable=False)  # basic / pro
    status: Mapped[str] = mapped_column(String(255), nullable=False, index=True)  # 取值见 OrderStatus
    started_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "product_tier": self.product_tier,
            "status": self.status,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
        }
