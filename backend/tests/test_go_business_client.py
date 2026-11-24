"""Go 业务服务客户端与服务层接入测试。

主线是「远程优先、失败降级」：
- 未配置 GO_BUSINESS_URL 时完全不发请求；
- 连接失败、非法 JSON、结构不完整一律按不可用返回 None；
- 服务层拿到远程结果直接采用，拿到 None 退回本地实现；
- 显式注入 now 的离线场景不走远程，保证时间口径不被远程时钟顶掉；
- 参数校验先于远程调用，非法入参不会被转发成另一个服务的错误。
"""

import io
import json
import time
import urllib.error

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Ticket
from backend.models.ticket import TicketValidationError
from backend.services import go_client, orders, tickets

REMOTE_URL = "http://127.0.0.1:8082"


class _FakeOpener:
    """替换 go_client._opener：记录请求、按需返回字节或抛错。"""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.urls = []

    def open(self, url, timeout=None):
        self.urls.append(url)
        if self.error:
            raise self.error
        return io.BytesIO(self.payload if self.payload is not None else b"{}")


@pytest.fixture()
def ticket_db(tmp_path, monkeypatch):
    """把工单服务指向临时 SQLite，不污染真实 app.db。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(tickets, "SessionLocal", factory)

    yield factory

    engine.dispose()


def _insert_pending(factory, ticket_id="T-1", user_id="u1", created_at=None):
    """直接落一张待确认草稿，避开建单的锁与状态机路径。"""
    db = factory()
    try:
        db.add(
            Ticket(
                id=ticket_id,
                dedup_key=f"dedup-{ticket_id}",
                user_id=user_id,
                user_name="测试用户",
                description="专业版导出失败",
                tried_steps="",
                evidence="",
                status=tickets.STATUS_PENDING_CONFIRM,
                created_at=time.time() if created_at is None else created_at,
                replies=[],
            )
        )
        db.commit()
    finally:
        db.close()


def _remote_result():
    return {
        "items": [],
        "total": 0,
        "expired": 0,
        "timeout_seconds": 60,
        "deadline": 1_700_000_000.0,
    }


def test_pending_tickets_without_config_never_calls_network(monkeypatch):
    monkeypatch.delenv("GO_BUSINESS_URL", raising=False)
    opener = _FakeOpener()
    monkeypatch.setattr(go_client, "_opener", opener)

    assert go_client.business_enabled() is False
    assert go_client.pending_tickets(user_id="u1") is None
    assert opener.urls == []


def test_pending_tickets_degrades_on_network_failure(monkeypatch):
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    monkeypatch.setattr(
        go_client, "_opener", _FakeOpener(error=urllib.error.URLError("refused"))
    )

    assert go_client.pending_tickets(user_id="u1") is None


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"[]", b'{"total": 0}', b'{"items": {}}'],
)
def test_pending_tickets_degrades_on_invalid_payload(monkeypatch, payload):
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    monkeypatch.setattr(go_client, "_opener", _FakeOpener(payload=payload))

    assert go_client.pending_tickets(user_id="u1") is None


def test_pending_tickets_returns_result_and_encodes_parameters(monkeypatch):
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    result = _remote_result()
    opener = _FakeOpener(payload=json.dumps(result).encode("utf-8"))
    monkeypatch.setattr(go_client, "_opener", opener)

    got = go_client.pending_tickets(
        user_id="u 1", include_expired=True, timeout_seconds=60
    )

    assert got == result
    assert len(opener.urls) == 1
    url = opener.urls[0]
    assert "/api/tickets/pending?" in url
    assert "user_id=u+1" in url
    assert "include_expired=true" in url
    assert "timeout_seconds=60" in url


def test_order_query_prefers_remote_result(monkeypatch):
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    result = {"has_order": True, "order_id": "O-REMOTE"}
    monkeypatch.setattr(
        go_client, "_opener", _FakeOpener(payload=json.dumps(result).encode("utf-8"))
    )
    monkeypatch.setattr(
        orders, "SessionLocal", lambda: pytest.fail("命中远程结果时不应查库")
    )

    assert orders.query_order("u1") == result


def test_order_query_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    monkeypatch.setattr(
        go_client, "_opener", _FakeOpener(error=urllib.error.URLError("refused"))
    )
    calls = []

    def fake_get_order(user_id):
        calls.append(user_id)
        return None

    monkeypatch.setattr(orders, "get_order", fake_get_order)

    got = orders.query_order("u1")

    assert calls == ["u1"]
    assert got["has_order"] is False


def test_list_pending_tickets_prefers_remote_result(monkeypatch):
    result = _remote_result()
    calls = []

    def fake_remote(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(go_client, "pending_tickets", fake_remote)
    monkeypatch.setattr(
        tickets, "SessionLocal", lambda: pytest.fail("命中远程结果时不应查库")
    )

    got = tickets.list_pending_tickets(
        user_id="u1", include_expired=True, timeout_seconds=60
    )

    assert got is result
    assert calls == [
        {"user_id": "u1", "include_expired": True, "timeout_seconds": 60}
    ]


def test_list_pending_tickets_falls_back_to_local(monkeypatch, ticket_db):
    _insert_pending(ticket_db)
    monkeypatch.setattr(go_client, "pending_tickets", lambda **kwargs: None)

    got = tickets.list_pending_tickets(user_id="u1")

    assert got["total"] == 1
    assert got["items"][0]["id"] == "T-1"
    assert got["items"][0]["expired"] is False
    assert set(got) == {"items", "total", "expired", "timeout_seconds", "deadline"}


def test_list_pending_tickets_skips_remote_when_now_is_injected(monkeypatch, ticket_db):
    """注入时间属于本地确定性路径，远程服务用自己的时钟无法对齐。"""
    now = 1_700_000_000.0
    _insert_pending(ticket_db, created_at=now - 10)

    def forbidden_remote(**kwargs):
        pytest.fail("注入 now 时不应走远程")

    monkeypatch.setattr(go_client, "pending_tickets", forbidden_remote)

    got = tickets.list_pending_tickets(now=now)

    assert got["total"] == 1
    assert got["deadline"] == now - tickets.PENDING_CONFIRM_TIMEOUT_SECONDS
    assert got["items"][0]["remaining_seconds"] == (
        tickets.PENDING_CONFIRM_TIMEOUT_SECONDS - 10
    )


def test_list_pending_tickets_validates_before_remote(monkeypatch):
    def forbidden_remote(**kwargs):
        pytest.fail("非法入参不应转发给远程服务")

    monkeypatch.setattr(go_client, "pending_tickets", forbidden_remote)

    with pytest.raises(TicketValidationError):
        tickets.list_pending_tickets(timeout_seconds=-1)
