"""工单幂等建单与并发安全测试。

覆盖三类场景：
1. 多线程同问题并发建单 —— 仅产生一个工单，其余请求命中已有工单。
2. 重复建单（顺序）—— 返回原工单且 created=False。
3. 参数非法 —— 空描述 / 全空白描述被拒绝。

测试使用独立的临时 SQLite 库，不污染真实 app.db。
"""

import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Ticket
from backend.models.ticket import TicketErrorCode, TicketValidationError
from backend.services import tickets


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """构造指向临时 SQLite 的 sessionmaker，并替换服务层引用的 SessionLocal。"""
    db_file = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(tickets, "SessionLocal", test_session)

    yield test_session

    engine.dispose()


def _count_tickets(session) -> int:
    return session().query(Ticket).count()


def test_并发同问题建单只产生一个工单(db_env):
    """多线程同时对同一用户同一问题建单，最终只落一个工单。"""
    results = []
    lock = threading.Lock()

    def worker():
        ticket, created = tickets.create_ticket(
            user_id="u-conc",
            user_name="并发用户",
            description="并发建单测试问题",
            tried_steps="重启客户端",
            evidence="无",
        )
        with lock:
            results.append(created)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 10 个并发请求中恰好 1 个新建，其余 9 个命中已有
    assert results.count(True) == 1
    assert results.count(False) == 9
    assert _count_tickets(db_env) == 1


def test_重复建单返回原工单且标记非新建(db_env):
    """顺序重复建单，第二次起返回原工单且 created=False。"""
    first, created_first = tickets.create_ticket(
        user_id="u-dup",
        user_name="重复用户",
        description="顺序重复建单测试问题",
        tried_steps="",
        evidence="",
    )
    second, created_second = tickets.create_ticket(
        user_id="u-dup",
        user_name="重复用户",
        description="顺序重复建单测试问题",
        tried_steps="",
        evidence="",
    )

    assert created_first is True
    assert created_second is False
    assert first["id"] == second["id"]
    assert _count_tickets(db_env) == 1


def test_不同描述生成不同工单(db_env):
    """不同问题描述应各自成单，不互相幂等命中。"""
    _, c1 = tickets.create_ticket("u-a", "用户A", "问题一", "", "")
    _, c2 = tickets.create_ticket("u-a", "用户A", "问题二", "", "")

    assert c1 is True
    assert c2 is True
    assert _count_tickets(db_env) == 2


def test_空描述被拒绝(db_env):
    """空字符串描述直接抛入参校验异常，错误码指明是描述为空。"""
    with pytest.raises(TicketValidationError) as exc:
        tickets.create_ticket("u-x", "用户X", "", "", "")

    assert exc.value.message == "问题描述不能为空"
    assert exc.value.code == TicketErrorCode.INVALID_DESCRIPTION


def test_全空白描述被拒绝(db_env):
    """全空白描述（空格/换行）strip 后为空，同样被拒绝。"""
    with pytest.raises(TicketValidationError) as exc:
        tickets.create_ticket("u-x", "用户X", "   \n\t  ", "", "")

    assert exc.value.code == TicketErrorCode.INVALID_DESCRIPTION


def test_描述前后空白被清理(db_env):
    """描述前后空白应被 strip，等价描述命中同一工单。"""
    _, c1 = tickets.create_ticket("u-s", "用户S", "  同一问题  ", "", "")
    _, c2 = tickets.create_ticket("u-s", "用户S", "同一问题", "", "")

    assert c1 is True
    assert c2 is False
    assert _count_tickets(db_env) == 1
