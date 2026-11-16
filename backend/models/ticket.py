from enum import Enum

from sqlalchemy import JSON, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class TicketStatus(str, Enum):
    """工单状态枚举。

    状态含义：
    - open：待处理（建单后的初始状态，或转人工前挂起的来源）
    - in_progress：处理中（客服开始跟进）
    - resolved：已解决（问题闭环）
    - pending_confirm：待确认（两阶段建单中，草稿等待用户确认）
    - transferred_human：已转人工（超出服务范围或排查无效）
    - closed：已关闭（确认前取消、超时收敛等终态）
    """

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    PENDING_CONFIRM = "pending_confirm"
    TRANSFERRED_HUMAN = "transferred_human"
    CLOSED = "closed"


# 合法状态流转表：key 为当前状态，value 为允许流转到的目标状态集合。
# 未出现在映射中的流转一律视为非法，由状态校验函数统一拒绝。
VALID_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.OPEN: {
        TicketStatus.IN_PROGRESS,
        TicketStatus.RESOLVED,
        TicketStatus.TRANSFERRED_HUMAN,
        TicketStatus.CLOSED,
    },
    TicketStatus.IN_PROGRESS: {
        TicketStatus.RESOLVED,
        TicketStatus.TRANSFERRED_HUMAN,
        TicketStatus.CLOSED,
    },
    TicketStatus.RESOLVED: {TicketStatus.CLOSED},
    TicketStatus.PENDING_CONFIRM: {
        TicketStatus.OPEN,
        TicketStatus.CLOSED,
    },
    TicketStatus.TRANSFERRED_HUMAN: {
        TicketStatus.IN_PROGRESS,
        TicketStatus.RESOLVED,
        TicketStatus.CLOSED,
    },
    TicketStatus.CLOSED: set(),
}


# 合法状态值集合：入参校验只需判断成员关系，避免每次调用重新构造集合
VALID_STATUS_VALUES: frozenset[str] = frozenset(status.value for status in TicketStatus)


def can_transition(current: TicketStatus, target: TicketStatus) -> bool:
    """判断工单能否从 current 状态流转到 target 状态。"""
    return target in VALID_TRANSITIONS.get(current, set())


def is_valid_status(value: str) -> bool:
    """判断字符串是否为合法工单状态值。"""
    return value in VALID_STATUS_VALUES


class TicketErrorCode:
    """工单业务错误码。

    错误码是接口契约的一部分：提示文案可以随产品调整，错误码保持不变，
    调用方按错误码分支处理，不必解析中文文案。
    命名规则为 TICKET_ 前缀 + 语义，可与 HTTP 状态码并列使用。
    """

    # 身份与权限
    UNAUTHORIZED = "TICKET_UNAUTHORIZED"
    FORBIDDEN = "TICKET_FORBIDDEN"
    # 资源
    NOT_FOUND = "TICKET_NOT_FOUND"
    # 状态机
    INVALID_TRANSITION = "TICKET_INVALID_TRANSITION"
    UNKNOWN_STATUS = "TICKET_UNKNOWN_STATUS"
    # 入参
    INVALID_PAYLOAD = "TICKET_INVALID_PAYLOAD"
    INVALID_DESCRIPTION = "TICKET_INVALID_DESCRIPTION"
    INVALID_STATUS_FILTER = "TICKET_INVALID_STATUS_FILTER"
    INVALID_PAGINATION = "TICKET_INVALID_PAGINATION"
    INVALID_ORDER = "TICKET_INVALID_ORDER"
    INVALID_KEYWORD = "TICKET_INVALID_KEYWORD"
    INVALID_BATCH = "TICKET_INVALID_BATCH"
    INVALID_STATS_RANGE = "TICKET_INVALID_STATS_RANGE"
    # 业务规则
    TRANSFER_RULE_NOT_MATCHED = "TICKET_TRANSFER_RULE_NOT_MATCHED"


class TicketBusinessError(Exception):
    """工单业务异常基类：统一携带错误码、提示文案、HTTP 状态与补充上下文。

    工单模块内可预期的失败（未登录、无权限、工单不存在、入参非法、流转非法、
    规则未命中）一律以本类派生的异常表达，接口层在同一个地方转成统一响应，
    业务代码不再各自拼装 {"error": ...} 字典，避免同类错误在不同接口里
    出现不同字段、不同文案。
    """

    code = TicketErrorCode.INVALID_PAYLOAD
    http_status = 400
    default_message = "工单操作失败"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict | None = None,
        http_status: int | None = None,
    ):
        self.code = code or type(self).code
        self.http_status = http_status or type(self).http_status
        self.message = message or type(self).default_message
        self.details = dict(details or {})
        super().__init__(self.message)

    def to_payload(self) -> dict:
        """转为统一的错误响应体。

        error 为可直接展示的中文文案（沿用既有字段名，前端无需改解析方式），
        error_code 为机器可读标识；error_detail 只在有补充上下文时出现，
        例如非法流转的原/目标状态、未命中规则时的判定结论。
        """
        payload = {"error": self.message, "error_code": self.code}
        if self.details:
            payload["error_detail"] = self.details
        return payload


class TicketAuthRequiredError(TicketBusinessError):
    """登录态缺失或无效。"""

    code = TicketErrorCode.UNAUTHORIZED
    http_status = 401
    default_message = "未登录"


class TicketPermissionError(TicketBusinessError):
    """权限不足：普通用户调用管理操作，或访问他人工单。"""

    code = TicketErrorCode.FORBIDDEN
    http_status = 403
    default_message = "无权限"


class TicketNotFoundError(TicketBusinessError):
    """工单不存在。"""

    code = TicketErrorCode.NOT_FOUND
    http_status = 404
    default_message = "工单不存在"

    def __init__(self, ticket_id: str | None = None, message: str | None = None):
        details = {"ticket_id": ticket_id} if ticket_id else None
        super().__init__(message, details=details)


class TicketValidationError(TicketBusinessError, ValueError):
    """入参校验失败。

    同时继承 ValueError，兼容按通用入参错误捕获的既有调用方与测试；
    具体是哪种入参问题由错误码区分。
    """

    code = TicketErrorCode.INVALID_PAYLOAD
    http_status = 422
    default_message = "工单入参不合法"


class TicketRuleError(TicketBusinessError):
    """业务规则未满足，例如规则判定未命中转人工条件。"""

    code = TicketErrorCode.TRANSFER_RULE_NOT_MATCHED
    http_status = 409
    default_message = "未命中转人工规则"


class TicketTransitionError(TicketBusinessError):
    """工单状态非法流转业务异常。

    当请求的流转不在合法流转表中，或涉及未知状态值时抛出。
    原状态与目标状态放在错误详情里，调用方既能展示文案，也能按错误码分支处理。
    """

    code = TicketErrorCode.INVALID_TRANSITION
    http_status = 409
    default_message = "工单状态流转非法"

    def __init__(
        self,
        current: str,
        target: str,
        message: str | None = None,
        code: str | None = None,
    ):
        self.current = current
        self.target = target
        super().__init__(
            message or f"非法状态流转：{current} -> {target}",
            code=code,
            details={"from_status": current, "to_status": target},
            http_status=422 if code == TicketErrorCode.UNKNOWN_STATUS else None,
        )


def _as_status(value) -> TicketStatus | None:
    """把入参解析为状态枚举：已是枚举直接返回，合法字符串转枚举，其余返回 None。"""
    if isinstance(value, TicketStatus):
        return value
    if isinstance(value, str) and is_valid_status(value):
        return TicketStatus(value)
    return None


def _status_text(value) -> str:
    """把无法识别的状态入参转成可放进提示与错误详情的文本（None 记空串）。"""
    return "" if value is None else str(value)


def validate_transition(
    current: TicketStatus | str,
    target: TicketStatus | str,
) -> None:
    """校验工单状态流转是否合法，非法时抛出 TicketTransitionError。

    入参同时接受枚举或字符串：非法值先统一按「未知状态」拒绝，再走合法流转表，
    保证「未知状态」「空值」与「非法流转」三类输入都能得到明确错误，
    而不是在取枚举成员时抛出与业务无关的属性异常。
    """
    resolved_current = _as_status(current)
    if resolved_current is None:
        raise TicketTransitionError(
            _status_text(current),
            _status_text(target),
            message=f"未知的当前状态：{_status_text(current) or '空值'}",
            code=TicketErrorCode.UNKNOWN_STATUS,
        )

    resolved_target = _as_status(target)
    if resolved_target is None:
        raise TicketTransitionError(
            resolved_current.value,
            _status_text(target),
            message=f"未知的目标状态：{_status_text(target) or '空值'}",
            code=TicketErrorCode.UNKNOWN_STATUS,
        )

    if not can_transition(resolved_current, resolved_target):
        raise TicketTransitionError(resolved_current.value, resolved_target.value)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # user_id / status / created_at 是列表筛选、超时扫描与统计聚合的高频条件，
    # 各建单列索引让这些查询走索引定位而不是全表扫描
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    tried_steps: Mapped[str] = mapped_column(String, default="")
    evidence: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="open", index=True)  # 取值见 TicketStatus
    created_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    replies: Mapped[list] = mapped_column(JSON, default=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "dedup_key": self.dedup_key,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "description": self.description,
            "tried_steps": self.tried_steps,
            "evidence": self.evidence,
            "status": self.status,
            "created_at": self.created_at,
            "replies": self.replies,
        }


class TicketStatusLog(Base):
    """工单状态变更时间线记录。

    每次工单状态变化（建单、流转、转人工、取消等）落一条日志，
    记录原状态、新状态、操作人与变更时间，供详情页还原完整处理轨迹。
    原状态为「初始创建」时 from_status 记为空字符串，表示该条是建单首条。
    """

    __tablename__ = "ticket_status_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String, default="")
    to_status: Mapped[str] = mapped_column(String, nullable=False)
    operator: Mapped[str] = mapped_column(String, default="system")
    changed_at: Mapped[float] = mapped_column(Float, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "operator": self.operator,
            "changed_at": self.changed_at,
        }
