"""Go 工单写入客户端请求与错误响应测试。"""

import io
import json
import urllib.error

import pytest
from backend.models.ticket import TicketErrorCode
from backend.services import go_client
REMOTE_URL = "http://127.0.0.1:8082"


class _FakeResponse(io.BytesIO):
    """补上 .status 的最小响应对象：go_client 要读状态码，BytesIO 没有。"""

    def __init__(self, payload: bytes, status: int = 200):
        super().__init__(payload)
        self.status = status


class _FakeOpener:
    """替换 go_client._opener：记录请求，按需返回响应、状态码或抛错。"""

    def __init__(self, payload=None, status=200, error=None):
        self.payload = payload if payload is not None else b"{}"
        self.status = status
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return _FakeResponse(self.payload, self.status)


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    """构造 urllib 的 HTTPError：fp 必须可读，否则拿不到业务错误体。"""
    return urllib.error.HTTPError(
        f"{REMOTE_URL}/api/tickets/confirm",
        code,
        "error",
        {},
        io.BytesIO(json.dumps(body).encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# 客户端层：请求形状与四种响应
# ---------------------------------------------------------------------------


def test_write_calls_without_config_never_touch_network(monkeypatch):
    monkeypatch.delenv("GO_BUSINESS_URL", raising=False)
    opener = _FakeOpener()
    monkeypatch.setattr(go_client, "_opener", opener)

    assert go_client.confirm_ticket("T-1", "李四") is None
    assert go_client.cancel_ticket("T-1", "不要了", "李四") is None
    assert go_client.update_ticket_status("T-1", "resolved") is None
    assert opener.requests == []


@pytest.mark.parametrize(
    "call,path,expected",
    [
        (lambda: go_client.confirm_ticket("T-1", "李四"), "/api/tickets/confirm",
         {"ticket_id": "T-1", "operator": "李四"}),
        (lambda: go_client.cancel_ticket("T-1", "不要了", "李四"), "/api/tickets/cancel",
         {"ticket_id": "T-1", "reason": "不要了", "operator": "李四"}),
        (lambda: go_client.update_ticket_status("T-1", "resolved"), "/api/tickets/status",
         {"ticket_id": "T-1", "status": "resolved"}),
    ],
)
def test_write_calls_use_expected_path_and_payload(monkeypatch, call, path, expected):
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    opener = _FakeOpener(payload=json.dumps({"id": "T-1"}).encode("utf-8"))
    monkeypatch.setattr(go_client, "_opener", opener)

    result = call()

    assert result["status"] == 200
    assert result["body"]["id"] == "T-1"
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == f"{REMOTE_URL}{path}"
    assert request.method == "POST"
    assert json.loads(request.data.decode("utf-8")) == expected


def test_write_call_reads_business_error_body_from_4xx(monkeypatch):
    """核心行为：409 的响应体必须被读出来，而不是当成「服务不可用」。"""
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    monkeypatch.setattr(
        go_client,
        "_opener",
        _FakeOpener(
            error=_http_error(
                409,
                {
                    "error": "非法状态流转：closed -> open",
                    "error_code": TicketErrorCode.INVALID_TRANSITION,
                    "error_detail": {"from_status": "closed", "to_status": "open"},
                },
            )
        ),
    )

    result = go_client.confirm_ticket("T-1", "李四")

    assert result is not None
    assert result["status"] == 409
    assert result["body"]["error_code"] == TicketErrorCode.INVALID_TRANSITION


def test_write_call_degrades_when_service_unreachable(monkeypatch):
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    monkeypatch.setattr(
        go_client, "_opener", _FakeOpener(error=urllib.error.URLError("refused"))
    )

    assert go_client.confirm_ticket("T-1", "李四") is None


def test_write_call_degrades_when_4xx_body_is_not_json(monkeypatch):
    error = urllib.error.HTTPError(
        f"{REMOTE_URL}/api/tickets/confirm", 502, "bad gateway", {}, io.BytesIO(b"oops")
    )
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    monkeypatch.setattr(go_client, "_opener", _FakeOpener(error=error))

    assert go_client.confirm_ticket("T-1", "李四") is None


def test_write_call_skips_empty_ticket_id(monkeypatch):
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    opener = _FakeOpener()
    monkeypatch.setattr(go_client, "_opener", opener)

    assert go_client.confirm_ticket("", "李四") is None
    assert opener.requests == []


# ---------------------------------------------------------------------------
# 服务层：远程结论优先，无法识别才降级
# ---------------------------------------------------------------------------
