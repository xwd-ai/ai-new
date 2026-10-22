from enum import Enum

from sqlalchemy import JSON, Float, String
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
