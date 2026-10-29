"""同一 SQLite 数据库内，按资料指纹协调入库执行者。"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
import threading
import uuid

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from backend.logging_conf import log_event
from backend.models import KnowledgeIngestLease


LEASE_SECONDS = 30.0
HEARTBEAT_SECONDS = 10.0
# 时间在 SQLite 真正执行写语句时计算，避免锁等待期间过时的时间让旧租约复活。
_DB_NOW = (func.julianday("now") - 2440587.5) * 86400.0


class IngestBusyError(RuntimeError):
    pass


class IngestLeaseLostError(RuntimeError):
    pass


@dataclass
class Lease:
    fingerprint: str
    owner: str
    session_factory: Callable
    lost: threading.Event = field(default_factory=threading.Event)

    def refresh(self, db) -> None:
        """检查所有权并续租；调用方提交事务前持续持有 SQLite 写锁。"""
        if self.lost.is_set():
            raise IngestLeaseLostError("资料处理租约已失效，请稍后重试")
        result = db.execute(update(KnowledgeIngestLease).where(
            KnowledgeIngestLease.fingerprint == self.fingerprint,
            KnowledgeIngestLease.owner == self.owner,
            KnowledgeIngestLease.expires_at > _DB_NOW,
        ).values(expires_at=_DB_NOW + LEASE_SECONDS))
        if result.rowcount != 1:
            self.lost.set()
            raise IngestLeaseLostError("资料处理租约已失效，请稍后重试")

    def release(self) -> None:
        """只释放自己的租约；旧执行者不能释放后来接管的租约。"""
        try:
            with self.session_factory() as db:
                db.execute(delete(KnowledgeIngestLease).where(
                    KnowledgeIngestLease.fingerprint == self.fingerprint,
                    KnowledgeIngestLease.owner == self.owner,
                ))
                db.commit()
        except SQLAlchemyError as exc:
            # 清理失败由过期机制兜底，不把已经提交的入库误报为失败。
            log_event("ingest.lease_release", level=logging.WARNING, error_type=type(exc).__name__)


def _heartbeat(lease: Lease, stopped: threading.Event) -> None:
    while not stopped.wait(HEARTBEAT_SECONDS):
        try:
            with lease.session_factory() as db:
                lease.refresh(db)
                db.commit()
        except (SQLAlchemyError, IngestLeaseLostError) as exc:
            lease.lost.set()
            log_event("ingest.heartbeat", level=logging.WARNING, error_type=type(exc).__name__)
            return


@contextmanager
def hold(fingerprint: str, session_factory):
    owner = uuid.uuid4().hex
    statement = insert(KnowledgeIngestLease).values(
        fingerprint=fingerprint, owner=owner, expires_at=_DB_NOW + LEASE_SECONDS,
    ).on_conflict_do_update(
        index_elements=["fingerprint"],
        set_={"owner": owner, "expires_at": _DB_NOW + LEASE_SECONDS},
        where=KnowledgeIngestLease.expires_at <= _DB_NOW,
    )
    with session_factory() as db:
        result = db.execute(statement)
        db.commit()
        if result.rowcount != 1:
            raise IngestBusyError("资料正在处理中，请稍后重试")
    lease = Lease(fingerprint, owner, session_factory)
    stopped = threading.Event()
    thread = threading.Thread(target=_heartbeat, args=(lease, stopped), daemon=True)
    thread.start()
    try:
        yield lease
    finally:
        stopped.set()
        thread.join()
        lease.release()
