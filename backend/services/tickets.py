import hashlib
import time
from contextlib import contextmanager

from sqlalchemy import func, text

from backend.database import SessionLocal
from backend.models import Ticket

# 工单持久化到 SQLite 数据库
# 一个工单对应一次「问题 → 排查 → 建单」的闭环
# 工单状态机：open(待处理) -> in_progress(处理中) -> resolved(已解决)
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
) -> dict:
    key = _dedup_key(user_id, description)
    # 建单临界区接入 advisory lock：同一意图（dedup_key）的并发建单在此串行执行，
    # 先到者建单并提交，后到者重查即可命中已有工单，避免并发下重复建单。
    with advisory_lock(key) as db:
        existing = (
            db.query(Ticket)
            .filter(Ticket.dedup_key == key, Ticket.status != "resolved")
            .first()
        )
        if existing:
            return existing.to_dict()

        ticket = Ticket(
            id=_next_id(db),
            dedup_key=key,
            user_id=user_id,
            user_name=user_name,
            description=description,
            tried_steps=tried_steps,
            evidence=evidence,
            status="open",
            created_at=time.time(),
            replies=[],
        )
        db.add(ticket)
        db.flush()
        db.refresh(ticket)
        return ticket.to_dict()


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
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        replies = list(t.replies or [])
        replies.append({"replier": replier, "content": content, "at": time.time()})
        t.replies = replies
        if t.status == "open":
            t.status = "in_progress"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


def update_status(ticket_id: str, status: str) -> dict | None:
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        t.status = status
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()
