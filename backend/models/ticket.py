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


def can_transition(current: TicketStatus, target: TicketStatus) -> bool:
    """判断工单能否从 current 状态流转到 target 状态。"""
    return target in VALID_TRANSITIONS.get(current, set())


def is_valid_status(value: str) -> bool:
    """判断字符串是否为合法工单状态值。"""
    return value in {s.value for s in TicketStatus}


class TicketTransitionError(Exception):
    """工单状态非法流转业务异常。

    当请求的流转不在合法流转表中，或涉及未知状态值时抛出。
    调用方据此向用户返回明确的错误信息，而非让底层异常直接上抛。
    """

    def __init__(
        self,
        current: str,
        target: str,
        message: str | None = None,
    ):
        self.current = current
        self.target = target
        self.message = message or f"非法状态流转：{current} -> {target}"
        super().__init__(self.message)


def validate_transition(
    current: TicketStatus | str,
    target: TicketStatus | str,
) -> None:
    """校验工单状态流转是否合法，非法时抛出 TicketTransitionError。

    入参同时接受枚举或字符串：字符串会先校验是否为已知状态值，
    再解析为枚举后走合法流转表校验，保证「未知状态」与「非法流转」都能被明确拒绝。
    """
    if isinstance(current, str):
        if not is_valid_status(current):
            raise TicketTransitionError(current, target, message=f"未知的当前状态：{current}")
        current = TicketStatus(current)
    if isinstance(target, str):
        if not is_valid_status(target):
            raise TicketTransitionError(
                current.value, target, message=f"未知的目标状态：{target}"
            )
        target = TicketStatus(target)

    if not can_transition(current, target):
        raise TicketTransitionError(current.value, target.value)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    user_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    tried_steps: Mapped[str] = mapped_column(String, default="")
    evidence: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="open")  # open/in_progress/resolved
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
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
