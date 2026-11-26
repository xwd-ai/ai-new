"""工单状态流转的 Go 接入测试。

与只读接口（test_go_business_client.py）的关键差异：**4xx 不是降级信号**。
Go 返回的 404 / 409 / 422 是业务结论，必须翻译成与本地一致的返回值或异常；
只有「连不上、响应不是 JSON、错误码不认识」才退回本地实现。
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
from backend.models.ticket import TicketErrorCode, TicketStatus, TicketTransitionError
from backend.services import go_client
from backend.tests.reference_api import ticket_service as tickets

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


@pytest.fixture()
def ticket_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(tickets, "SessionLocal", factory)

    yield factory

    engine.dispose()


def _insert_pending(factory, ticket_id="T-1", status=None):
    db = factory()
    try:
        db.add(
            Ticket(
                id=ticket_id,
                dedup_key=f"dedup-{ticket_id}",
                user_id="u1",
                user_name="测试用户",
                description="专业版导出失败",
                tried_steps="",
                evidence="",
                status=status or tickets.STATUS_PENDING_CONFIRM,
                created_at=time.time(),
                replies=[],
            )
        )
        db.commit()
    finally:
        db.close()


def _ticket_body(**overrides):
    body = {
        "id": "T-1",
        "dedup_key": "dedup-T-1",
        "user_id": "u1",
        "user_name": "测试用户",
        "description": "专业版导出失败",
        "tried_steps": "",
        "evidence": "",
        "status": "open",
        "created_at": 1_700_000_000.0,
        "replies": [],
    }
    body.update(overrides)
    return body


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
    opener = _FakeOpener(payload=json.dumps(_ticket_body()).encode("utf-8"))
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


def test_confirm_prefers_remote_result(monkeypatch):
    monkeypatch.setattr(
        go_client,
        "confirm_ticket",
        lambda ticket_id, operator: {"status": 200, "body": _ticket_body()},
    )
    monkeypatch.setattr(
        tickets, "SessionLocal", lambda: pytest.fail("命中远程结果时不应查库")
    )

    got = tickets.confirm_ticket("T-1", "李四")

    assert got["id"] == "T-1"
    assert got["status"] == "open"


def test_confirm_remote_not_found_returns_none(monkeypatch):
    monkeypatch.setattr(
        go_client,
        "confirm_ticket",
        lambda ticket_id, operator: {
            "status": 404,
            "body": {"error": "工单不存在", "error_code": TicketErrorCode.NOT_FOUND},
        },
    )
    monkeypatch.setattr(
        tickets, "SessionLocal", lambda: pytest.fail("远程已确认不存在，不应再查库")
    )

    assert tickets.confirm_ticket("T-404", "李四") is None


def test_confirm_remote_invalid_transition_raises_same_exception(monkeypatch):
    monkeypatch.setattr(
        go_client,
        "confirm_ticket",
        lambda ticket_id, operator: {
            "status": 409,
            "body": {
                "error": "非法状态流转：closed -> open",
                "error_code": TicketErrorCode.INVALID_TRANSITION,
                "error_detail": {"from_status": "closed", "to_status": "open"},
            },
        },
    )
    monkeypatch.setattr(
        tickets, "SessionLocal", lambda: pytest.fail("远程已给出业务结论，不应降级")
    )

    with pytest.raises(TicketTransitionError) as excinfo:
        tickets.confirm_ticket("T-1", "李四")

    assert excinfo.value.code == TicketErrorCode.INVALID_TRANSITION
    assert excinfo.value.details == {"from_status": "closed", "to_status": "open"}
    assert "closed -> open" in excinfo.value.message


def test_confirm_remote_unknown_status_raises_with_422_semantics(monkeypatch):
    monkeypatch.setattr(
        go_client,
        "confirm_ticket",
        lambda ticket_id, operator: {
            "status": 422,
            "body": {
                "error": "未知的目标状态：archived",
                "error_code": TicketErrorCode.UNKNOWN_STATUS,
                "error_detail": {"from_status": "open", "to_status": "archived"},
            },
        },
    )

    with pytest.raises(TicketTransitionError) as excinfo:
        tickets.confirm_ticket("T-1", "李四")

    assert excinfo.value.code == TicketErrorCode.UNKNOWN_STATUS
    assert excinfo.value.http_status == 422


def test_confirm_falls_back_to_local_when_service_unreachable(monkeypatch, ticket_db):
    _insert_pending(ticket_db)
    monkeypatch.setenv("GO_BUSINESS_URL", REMOTE_URL)
    monkeypatch.setattr(
        go_client, "_opener", _FakeOpener(error=urllib.error.URLError("refused"))
    )

    got = tickets.confirm_ticket("T-1", "李四")

    assert got["status"] == "open"
    # 时间线必须留痕，证明走的是本地状态机而不是只改了字段
    db = ticket_db()
    try:
        assert any(
            log.from_status == tickets.STATUS_PENDING_CONFIRM
            and log.to_status == tickets.STATUS_OPEN
            for log in db.query(tickets.TicketStatusLog).all()
        )
    finally:
        db.close()


def test_confirm_falls_back_when_remote_error_is_unrecognized(monkeypatch, ticket_db):
    """5xx 不代表「工单状态如何」，应交给本地实现给出结论。"""
    _insert_pending(ticket_db)
    monkeypatch.setattr(
        go_client,
        "confirm_ticket",
        lambda ticket_id, operator: {
            "status": 500,
            "body": {"error": "工单操作失败", "error_code": "TICKET_INTERNAL"},
        },
    )

    got = tickets.confirm_ticket("T-1", "李四")

    assert got["status"] == "open"


def test_confirm_falls_back_when_remote_payload_incomplete(monkeypatch, ticket_db):
    """200 但缺 id：半截结果不能交给业务层。"""
    _insert_pending(ticket_db)
    monkeypatch.setattr(
        go_client,
        "confirm_ticket",
        lambda ticket_id, operator: {"status": 200, "body": {"status": "open"}},
    )

    got = tickets.confirm_ticket("T-1", "李四")

    assert got["id"] == "T-1"


def test_cancel_prefers_remote_result(monkeypatch):
    body = _ticket_body(
        status="closed",
        replies=[{"replier": "system", "content": "取消工单：不要了", "at": 1.0}],
    )
    monkeypatch.setattr(
        go_client,
        "cancel_ticket",
        lambda ticket_id, reason, operator: {"status": 200, "body": body},
    )
    monkeypatch.setattr(
        tickets, "SessionLocal", lambda: pytest.fail("命中远程结果时不应查库")
    )

    got = tickets.cancel_ticket("T-1", "不要了", "李四")

    assert got["status"] == "closed"
    assert got["replies"][0]["content"] == "取消工单：不要了"


def test_cancel_falls_back_to_local_appends_system_reply(monkeypatch, ticket_db):
    _insert_pending(ticket_db)
    monkeypatch.setattr(go_client, "cancel_ticket", lambda *args: None)

    got = tickets.cancel_ticket("T-1", "问题已解决", "李四")

    assert got["status"] == "closed"
    assert len(got["replies"]) == 1
    assert got["replies"][0]["replier"] == "system"
    assert got["replies"][0]["content"] == "取消工单：问题已解决"


def test_update_status_prefers_remote_result(monkeypatch):
    monkeypatch.setattr(
        go_client,
        "update_ticket_status",
        lambda ticket_id, status: {
            "status": 200,
            "body": _ticket_body(status="resolved"),
        },
    )
    monkeypatch.setattr(
        tickets, "SessionLocal", lambda: pytest.fail("命中远程结果时不应查库")
    )

    got = tickets.update_status("T-1", "resolved")

    assert got["status"] == "resolved"


def test_update_status_remote_invalid_transition_raises(monkeypatch):
    monkeypatch.setattr(
        go_client,
        "update_ticket_status",
        lambda ticket_id, status: {
            "status": 409,
            "body": {
                "error": "非法状态流转：closed -> open",
                "error_code": TicketErrorCode.INVALID_TRANSITION,
                "error_detail": {"from_status": "closed", "to_status": "open"},
            },
        },
    )

    with pytest.raises(TicketTransitionError):
        tickets.update_status("T-1", "open")


def test_update_status_falls_back_to_local(monkeypatch, ticket_db):
    _insert_pending(ticket_db, status=TicketStatus.OPEN.value)
    monkeypatch.setattr(go_client, "update_ticket_status", lambda *args: None)

    got = tickets.update_status("T-1", "resolved")

    assert got["status"] == "resolved"


def test_update_status_local_path_still_rejects_same_status(monkeypatch, ticket_db):
    """降级路径也要保留「同状态非法」：这是状态机而不是幂等接口。"""
    _insert_pending(ticket_db, status=TicketStatus.RESOLVED.value)
    monkeypatch.setattr(go_client, "update_ticket_status", lambda *args: None)

    with pytest.raises(TicketTransitionError):
        tickets.update_status("T-1", "resolved")


def test_operator_is_cleaned_before_remote(monkeypatch):
    seen = {}

    def fake_remote(ticket_id, operator):
        seen["operator"] = operator
        return {"status": 200, "body": _ticket_body()}

    monkeypatch.setattr(go_client, "confirm_ticket", fake_remote)

    tickets.confirm_ticket("T-1", "  李   四 ")

    assert seen["operator"] == "李 四"
