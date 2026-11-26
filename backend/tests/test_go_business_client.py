"""Go 业务客户端及订单查询的远程优先测试。"""

import io
import json
import urllib.error

import pytest
from backend.services import go_client, orders
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
