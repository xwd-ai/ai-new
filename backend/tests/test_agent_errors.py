"""错误分类、对外提示与对话降级的回归测试，不调用真实模型。"""

import json
import os
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from backend.api.chat import chat_router
from backend import providers
from backend.services import agent, chat, llm, tools


PRIVATE_DETAIL = "private-key-and-provider-response"


def _sdk_error(error_type, status):
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://example.invalid/chat")
    )
    return error_type(PRIVATE_DETAIL, response=response, body=None)


ERROR_CASES = [
    (TimeoutError(PRIVATE_DETAIL), "timeout", "超时", True),
    (
        APITimeoutError(request=httpx.Request("POST", "https://example.invalid")),
        "timeout", "超时", True,
    ),
    (ConnectionError(PRIVATE_DETAIL), "connection_error", "连接", True),
    (
        APIConnectionError(
            message=PRIVATE_DETAIL,
            request=httpx.Request("POST", "https://example.invalid"),
        ),
        "connection_error", "连接", True,
    ),
    (_sdk_error(RateLimitError, 429), "rate_limited", "繁忙", True),
    (_sdk_error(AuthenticationError, 401), "model_configuration", "管理员", False),
    (_sdk_error(PermissionDeniedError, 403), "model_configuration", "管理员", False),
    (_sdk_error(InternalServerError, 503), "upstream_unavailable", "暂时不可用", True),
    (_sdk_error(BadRequestError, 400), "model_request_rejected", "管理员", False),
    (RuntimeError(PRIVATE_DETAIL), "internal_error", "稍后", False),
]


@pytest.mark.parametrize("exc,code,hint,retryable", ERROR_CASES)
def test_model_failure_has_safe_classified_event(exc, code, hint, retryable):
    def fail(_state):
        raise exc

    state = agent.AgentState(query="导出失败")
    transition = agent.run_loop(state, step_fn=fail, search_fn=lambda _: [])

    assert transition.kind is agent.TransitionKind.MODEL_ERROR
    event = state.events[-1]
    assert event.get("error_code") == code
    assert event.get("retryable") is retryable
    assert hint in transition.detail
    assert event["message"] == transition.detail
    assert PRIVATE_DETAIL not in json.dumps(agent.summarize(state, transition).to_dict())


def test_retrieval_failure_is_safe_and_loop_can_continue():
    outputs = iter(["ACTION: search_knowledge 导出", "请补充导出格式。"])

    def fail(_query):
        raise TimeoutError(PRIVATE_DETAIL)

    state = agent.AgentState(query="导出失败")
    transition = agent.run_loop(state, step_fn=lambda _: next(outputs), search_fn=fail)

    assert transition.kind is agent.TransitionKind.COMPLETED
    error = next(e for e in state.events if e["kind"] == "error")
    assert error.get("error_code") == "timeout"
    assert "检索" in error["message"]
    assert PRIVATE_DETAIL not in json.dumps(state.observations + state.events)


def test_tool_failure_has_classification_without_raw_exception():
    registry = tools.ToolRegistry()

    def fail():
        raise ConnectionError(PRIVATE_DETAIL)

    registry.register("search_knowledge", "检索", fail)
    result = registry.execute("search_knowledge")

    assert not result.success
    assert getattr(result, "error_code", None) == "connection_error"
    assert getattr(result, "retryable", None) is True
    assert "连接" in result.error
    assert PRIVATE_DETAIL not in result.error


@pytest.fixture()
def chat_client(monkeypatch):
    # 隔离存储与检索，仅保留真实的 API → 编排 → Agent → provider 调用链。
    monkeypatch.setattr(chat, "get_or_create_conversation", lambda _: "test-conversation")
    monkeypatch.setattr(chat, "history", lambda _: [])
    monkeypatch.setattr(chat, "append_message", lambda *args: None)
    monkeypatch.setattr(chat, "append_tried_step", lambda *args: None)
    monkeypatch.setattr(chat, "current_user", lambda _: None)
    monkeypatch.setattr(chat.knowledge, "search_knowledge", lambda _: [])
    app = FastAPI()
    app.include_router(chat_router, prefix="/api")
    with chat.execution_scope(), TestClient(app) as client:
        yield client


def test_chat_api_uses_classified_failure_as_reply(chat_client, monkeypatch):
    class TimeoutProvider:
        def chat(self, *args, **kwargs):
            raise TimeoutError(PRIVATE_DETAIL)

    monkeypatch.setattr(chat, "_chat_provider", TimeoutProvider())
    response = chat_client.post("/api/chat", json={"message": "导出失败"})

    assert response.status_code == 200
    body = response.json()
    assert "超时" in body["reply"]
    error = next(e for e in body["trace"] if e["kind"] == "error")
    assert error["message"] == body["reply"]
    assert error["error_code"] == "timeout"
    assert PRIVATE_DETAIL not in response.text


@pytest.mark.parametrize("key", ["", "your-api-key", "sk-placeholder-not-real-key"])
def test_unconfigured_model_returns_actionable_hint(chat_client, monkeypatch, key):
    def unexpected_client():
        pytest.fail("Unconfigured model must not construct a network client")

    monkeypatch.setattr(providers, "OpenAICompatChatProvider", unexpected_client)
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    monkeypatch.setattr(chat, "_chat_provider", None)
    monkeypatch.setattr(llm, "_default_provider", None)
    response = chat_client.post("/api/chat", json={"message": "导出失败"})

    assert response.status_code == 200
    body = response.json()
    error = next(e for e in body["trace"] if e["kind"] == "error")
    assert error.get("error_code") == "model_configuration"
    assert "管理员" in body["reply"]


def test_chat_success_keeps_existing_response(chat_client, monkeypatch):
    class SuccessProvider:
        def chat(self, *args, **kwargs):
            return "请补充导出格式。"

    monkeypatch.setattr(chat, "_chat_provider", SuccessProvider())
    response = chat_client.post("/api/chat", json={"message": "导出失败"})

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "请补充导出格式。"
    assert body["sources"] == []
    assert body["trace"][-1]["kind"] == "done"


def test_initial_retrieval_failure_can_still_answer(chat_client, monkeypatch):
    calls = []

    def fail(query):
        calls.append(query)
        raise TimeoutError(PRIVATE_DETAIL)

    class SuccessProvider:
        def chat(self, *args, **kwargs):
            return "请补充导出格式。"

    monkeypatch.setattr(chat.knowledge, "search_knowledge", fail)
    monkeypatch.setattr(chat, "_chat_provider", SuccessProvider())
    response = chat_client.post("/api/chat", json={"message": "导出失败"})

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "请补充导出格式。"
    assert body["sources"] == []
    error = next(e for e in body["trace"] if e["kind"] == "error")
    assert error["error_code"] == "timeout"
    assert len(calls) == 1
    assert PRIVATE_DETAIL not in response.text


def test_chat_import_does_not_require_model_key():
    env = dict(os.environ, DEEPSEEK_API_KEY="", PYTHON_DOTENV_DISABLED="1")
    result = subprocess.run(
        [sys.executable, "-c", "from backend.services import chat"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_successful_retrieval_preserves_citations(chat_client, monkeypatch):
    calls = []

    def search(query):
        calls.append(query)
        return [{"id": "k-test", "title": "导出手册", "kind": "manual", "content": "使用 CSV。"}]

    class CitingProvider:
        def chat(self, *args, **kwargs):
            return '使用 CSV。<ref id="c1"/>'

    monkeypatch.setattr(chat.knowledge, "search_knowledge", search)
    monkeypatch.setattr(chat, "_chat_provider", CitingProvider())
    response = chat_client.post("/api/chat", json={"message": "导出失败"})

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "使用 CSV。[来源: 导出手册]"
    assert body["sources"] == [
        {"id": "k-test", "handle": "c1", "title": "导出手册", "kind": "manual"}
    ]
    assert len(calls) == 1
