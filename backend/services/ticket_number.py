"""使用数据库计数器分配工单号，调用方负责提交建单事务。"""

import re

from sqlalchemy import select, update
from sqlalchemy.dialects import mysql, postgresql, sqlite

from backend.models.ticket import Ticket, TicketNumberCounter

_COUNTER_ID = 1
_FIRST_NUMBER = 1001
_NUMBERED_TICKET = re.compile(r"T-([0-9]+)")


def _initialize_counter(db, lock: bool) -> None:
    """保留 T-数字 格式，首次从旧库最大数字序号继续。

    并发初始化由主键和 upsert 仲裁。之后每次直接更新计数器行，
    行锁（SQLite 为写锁）持有到工单事务结束，避免两个事务取得同一序号。
    """
    counter = TicketNumberCounter.__table__
    current = select(counter.c.last_number).where(counter.c.id == _COUNTER_ID)
    if db.execute(current.with_for_update() if lock else current).scalar_one_or_none() is None:
        last_number = _FIRST_NUMBER - 1
        for ticket_id in db.execute(select(Ticket.id)).scalars():
            match = _NUMBERED_TICKET.fullmatch(ticket_id)
            if match:
                last_number = max(last_number, int(match[1]))

        values = {"id": _COUNTER_ID, "last_number": last_number}
        dialect = db.get_bind().dialect.name
        if dialect == "mysql" or dialect == "mariadb":
            insert = mysql.insert(counter).values(**values)
            initialize = insert.on_duplicate_key_update(id=counter.c.id)
        else:
            insert = (sqlite.insert if dialect == "sqlite" else postgresql.insert)(
                counter
            ).values(**values)
            initialize = insert.on_conflict_do_nothing(index_elements=[counter.c.id])
        db.execute(initialize)


def lock_mysql_ticket_counter(db) -> None:
    _initialize_counter(db, lock=True)


def allocate_ticket_id(db) -> str:
    """取号与业务写入在同一事务提交。"""
    _initialize_counter(db, lock=db.get_bind().dialect.name == "mysql")
    counter = TicketNumberCounter.__table__
    current = select(counter.c.last_number).where(counter.c.id == _COUNTER_ID)
    db.execute(
        update(counter)
        .where(counter.c.id == _COUNTER_ID)
        .values(last_number=counter.c.last_number + 1)
    )
    # MySQL 的事务可能已建立一致性读快照，锁定读会返回刚更新的行。
    number = db.execute(current.with_for_update()).scalar_one()
    return f"T-{number}"
