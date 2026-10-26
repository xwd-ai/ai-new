import hashlib
import time
from contextlib import contextmanager

from sqlalchemy import func, text

from backend.database import SessionLocal
from backend.models import Ticket
from backend.models.ticket import TicketStatusLog, validate_transition

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


def _log_status_change(
    db,
    ticket_id: str,
    from_status: str,
    to_status: str,
    operator: str = "system",
) -> None:
    """向时间线写入一条状态变更记录。

    所有写接口在状态发生变化的同一事务内调用本函数，保证状态与
    时间线落痕一致提交；原状态为「初始创建」时 from_status 记空串。
    """
    db.add(
        TicketStatusLog(
            ticket_id=ticket_id,
            from_status=from_status,
            to_status=to_status,
            operator=operator,
            changed_at=time.time(),
        )
    )


def create_ticket(
    user_id: str,
    user_name: str,
    description: str,
    tried_steps: str,
    evidence: str,
) -> tuple[dict, bool]:
    """创建工单（两阶段建单的第一阶段），返回 (工单数据, 是否新建)。

    建单先落地为待确认草稿（pending_confirm），不直接进入待处理（open）：
    草稿等待用户二次确认后，再经确认接口流转为 open 正式工单。

    同一用户同一问题（dedup_key）已有未解决工单时，直接返回原工单，
    并标记 created=False，调用方据此区分「新建」与「命中已有」，避免重复建单。
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("问题描述不能为空")

    key = _dedup_key(user_id, description)
    # 两阶段建单：初始一律落待确认草稿，待确认接口流转为 open 后才成为正式工单
    initial_status = "pending_confirm"
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
        # 建单即首次状态落位，写入首条时间线记录（from_status 为空表示初始创建）
        _log_status_change(db, ticket.id, "", initial_status, operator="system")
        return ticket.to_dict(), True


def confirm_ticket(ticket_id: str) -> dict | None:
    """确认待确认草稿，将其正式流转为待处理工单（幂等）。

    两阶段建单的第二阶段：pending_confirm 草稿经用户二次确认后流转为 open。
    幂等语义：若工单已不在待确认状态（例如已 open、已关闭、已被并发确认），
    不抛错也不重复流转，直接原样返回当前工单，重复确认不会产生副作用。
    工单不存在时返回 None，由接口层区分「不存在」与「已确认」。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        # 已非待确认状态视为已确认/已终结，幂等返回，不重复流转
        if t.status != "pending_confirm":
            return t.to_dict()
        validate_transition(t.status, "open")
        _log_status_change(db, t.id, t.status, "open", operator="system")
        t.status = "open"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


def cancel_ticket(ticket_id: str, reason: str = "") -> dict | None:
    """取消待确认草稿，将其流转为已关闭终态，并记录取消原因。

    两阶段建单的取消分支：pending_confirm 草稿由用户主动放弃后流转为 closed。
    取消原因以 system 来源写入 replies，时间线同时落一条流转记录，供后续复盘。
    幂等语义：若工单已不在待确认状态（已 open、已关闭、已被并发确认或取消），
    不抛错也不重复流转，原样返回当前工单；工单不存在时返回 None。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        # 已非待确认状态视为已终结，幂等返回，不重复流转
        if t.status != "pending_confirm":
            return t.to_dict()
        validate_transition(t.status, "closed")
        replies = list(t.replies or [])
        entry = {"replier": "system", "content": f"取消工单：{reason}".strip(), "at": time.time()}
        replies.append(entry)
        t.replies = replies
        _log_status_change(db, t.id, t.status, "closed", operator="system")
        t.status = "closed"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


def list_tickets() -> list[dict]:
    db = SessionLocal()
    try:
        tickets = db.query(Ticket).order_by(Ticket.created_at.desc()).all()
        return [t.to_dict() for t in tickets]
    finally:
        db.close()


def _status_timeline(db, ticket_id: str) -> list[dict]:
    """查询工单的完整状态变更时间线，按发生时间升序返回。

    时间线记录建单落位与每次状态流转，详情页据此还原完整处理轨迹。
    """
    logs = (
        db.query(TicketStatusLog)
        .filter(TicketStatusLog.ticket_id == ticket_id)
        .order_by(TicketStatusLog.changed_at.asc(), TicketStatusLog.id.asc())
        .all()
    )
    return [log.to_dict() for log in logs]


def get_ticket(ticket_id: str) -> dict | None:
    """查询工单详情，附带完整状态时间线（按时间升序）。

    返回的工单字典在原有字段基础上追加 status_timeline 列表，
    供详情页还原「建单 → 每次流转」的完整处理轨迹。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        data = t.to_dict()
        data["status_timeline"] = _status_timeline(db, ticket_id)
        return data
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
            _log_status_change(db, t.id, t.status, "in_progress", operator=replier)
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
        _log_status_change(db, t.id, t.status, status, operator="system")
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
        _log_status_change(db, t.id, t.status, "transferred_human", operator="system")
        t.status = "transferred_human"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()
