import hashlib
import time
from contextlib import contextmanager

from sqlalchemy import func, text

from backend.database import SessionLocal
from backend.models import Ticket
from backend.models.ticket import validate_transition

# 工单持久化到 SQLite 数据库
# 一个工单对应一次「问题 → 排查 → 建单」的闭环
# 工单状态机：pending_confirm(待确认) -> open(待处理) -> in_progress(处理中) -> resolved(已解决)
# 建单前可挂起为 pending_confirm，等待用户确认后再进入 open
# 预置工单逻辑收敛到 backend.seed 模块


def _dedup_key(user_id: str, description: str) -> str:
    return hashlib.sha256(f"{user_id}::{description}".encode()).hexdigest()


@contextmanager
def advisory_lock(key: str):
    """建单 advisory lock：串行化同一意图（dedup_key）的并发建单。

    SQLite 无 PG 的 advisory lock 原语，这里借数据库连接级的写锁实现等价效果：
    持锁期间独占建单临界区，同一 key 的重复请求排队串行执行，避免并发下重复建单。
    """
    db = SessionLocal()
    try:
        # BEGIN IMMEDIATE 立即获取写锁，其他会话的写操作在此阻塞直到本事务提交/回滚
        db.execute(text("BEGIN IMMEDIATE"))
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _next_id(db) -> str:
    """工单号自增：基于现有最大序号 +1。"""
    max_id = db.query(func.max(Ticket.id)).scalar()
    if not max_id:
        seq = 1000
    else:
        seq = int(max_id.replace("T-", ""))
    return f"T-{seq + 1}"


def create_ticket(
    user_id: str,
    user_name: str,
    description: str,
    tried_steps: str,
    evidence: str,
    pending: bool = False,
) -> tuple[dict, bool]:
    """创建工单，返回 (工单数据, 是否新建)。

    同一用户同一问题（dedup_key）已有未解决工单时，直接返回原工单，
    并标记 created=False，调用方据此区分「新建」与「命中已有」，避免重复建单。

    pending=True 时表示「建单前挂起」，工单初始进入待确认状态（pending_confirm），
    等待用户二次确认后才流转为待处理（open）；默认直接以 open 状态建单。
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("问题描述不能为空")

    key = _dedup_key(user_id, description)
    initial_status = "pending_confirm" if pending else "open"
    # 建单临界区接入 advisory lock：同一意图（dedup_key）的并发建单在此串行执行，
    # 先到者建单并提交，后到者重查即可命中已有工单，避免并发下重复建单。
    with advisory_lock(key) as db:
        existing = (
            db.query(Ticket)
            .filter(Ticket.dedup_key == key, Ticket.status != "resolved")
            .first()
        )
        if existing:
            return existing.to_dict(), False

        ticket = Ticket(
            id=_next_id(db),
            dedup_key=key,
            user_id=user_id,
            user_name=user_name,
            description=description,
            tried_steps=tried_steps,
            evidence=evidence,
            status=initial_status,
            created_at=time.time(),
            replies=[],
        )
        db.add(ticket)
        db.flush()
        db.refresh(ticket)
        return ticket.to_dict(), True


def list_tickets() -> list[dict]:
    db = SessionLocal()
    try:
        tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).all()
        return [t.to_dict() for t in tickets]
    finally:
        db.close()


def get_ticket(ticket_id: str) -> dict | None:
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        return t.to_dict() if t else None
    finally:
        db.close()


def add_reply(ticket_id: str, replier: str, content: str) -> dict | None:
    """追加人工回复，并将待处理工单推进到处理中。

    状态变更统一走流转校验：仅 open 状态允许伴随回复自动流转到 in_progress，
    其余状态回复时不改变状态（保持当前状态不变），非法流转由校验函数拒绝。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        replies = list(t.replies or [])
        replies.append({"replier": replier, "content": content, "at": time.time()})
        t.replies = replies
        if t.status != "in_progress":
            validate_transition(t.status, "in_progress")
            t.status = "in_progress"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


def update_status(ticket_id: str, status: str) -> dict | None:
    """更新工单状态，变更前统一经过合法流转校验。

    未知状态值或非法流转会抛出 TicketTransitionError，由接口层捕获后
    返回明确错误信息，避免非法状态写入数据库。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        validate_transition(t.status, status)
        t.status = status
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


def transfer_to_human(ticket_id: str, reason: str = "") -> dict | None:
    """将工单转为人工处理，并把转人工原因写入回复时间线。

    转人工作为独立业务动作：入参原因会以 system 来源记录到 replies，
    随后工单状态流转到 transferred_human。状态变更统一经过流转校验，
    非法来源状态（如已关闭、已转人工的工单）由校验函数拒绝。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        validate_transition(t.status, "transferred_human")
        replies = list(t.replies or [])
        entry = {"replier": "system", "content": f"转人工：{reason}".strip(), "at": time.time()}
        replies.append(entry)
        t.replies = replies
        t.status = "transferred_human"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()
