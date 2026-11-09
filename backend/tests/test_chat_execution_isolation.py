import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
import threading
from types import SimpleNamespace

import pytest

from backend.services import chat


@pytest.fixture()
def isolated_chat(monkeypatch):
    # 保留真实 Agent 循环与回复编排，只隔离模型、查询和持久化。
    monkeypatch.setattr(chat, 'get_or_create_conversation', lambda user: user)
    monkeypatch.setattr(chat, 'history', lambda _: [])
    monkeypatch.setattr(chat, 'append_turn', lambda *args: None)
    monkeypatch.setattr(chat, 'append_tried_step', lambda *args: None)
    monkeypatch.setattr(chat.orders, 'query_order', lambda user: None)
    monkeypatch.setattr(chat.knowledge, 'search_knowledge', lambda query: [])
    monkeypatch.setattr(chat.llm, 'chat_step', lambda message, **kwargs: 'answer:' + message)
    monkeypatch.setattr(chat.escalation, 'decide_escalation', lambda message, **kwargs:
                        SimpleNamespace(suggest=message=='first', marker=message))
    with chat.execution_scope():
        yield


def test_parallel_executions_do_not_share_last_summary_or_decision(isolated_chat):
    barrier = threading.Barrier(2)
    def execute(message):
        reply, _, suggest = chat.build_reply(message, message)
        barrier.wait(timeout=5)  # 两次都完成写入后再读取，稳定复现原共享变量问题。
        return reply, suggest, chat.last_summary().answer, chat.last_escalation().marker
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, message) for message in ('first', 'second')]
        first, second = [future.result(timeout=10) for future in futures]
    assert first == ('answer:first', True, 'answer:first', 'first')
    assert second == ('answer:second', False, 'answer:second', 'second')


def test_new_context_has_no_other_contexts_last_result(isolated_chat):
    chat.build_reply('user', 'first')
    assert Context().run(chat.last_summary) is None
    assert Context().run(chat.last_escalation) is None


def test_failed_execution_clears_previous_result(isolated_chat, monkeypatch):
    chat.build_reply('user', 'first')
    def fail(*args): raise RuntimeError('database unavailable')
    monkeypatch.setattr(chat, 'get_or_create_conversation', fail)
    with pytest.raises(RuntimeError): chat.build_reply('user', 'second')
    assert chat.last_summary() is None and chat.last_escalation() is None


def test_http_request_scope_restores_callers_context(isolated_chat, monkeypatch):
    from backend.api.chat import chat as endpoint
    from backend.schemas import ChatRequest
    chat.build_reply('user', 'outer')
    outer = chat.last_summary()
    result = endpoint(ChatRequest(message='first'), user={'id': 'user'})
    assert result.reply == 'answer:first' and result.trace[-1]['kind'] == 'done'
    assert chat.last_summary() is outer
    assert chat.last_escalation().marker == 'outer'


def test_async_contexts_can_read_their_own_completed_execution(isolated_chat):
    async def scenario():
        first_ready, second_ready = asyncio.Event(), asyncio.Event()
        async def first():
            chat.build_reply('first', 'first')
            first_ready.set()
            await second_ready.wait()
            return chat.last_summary().answer
        async def second():
            await first_ready.wait()
            chat.build_reply('second', 'second')
            second_ready.set()
            return chat.last_summary().answer
        return await asyncio.gather(first(), second())
    assert asyncio.run(scenario()) == ['answer:first', 'answer:second']


def test_http_parallel_trace_is_bound_to_its_own_reply(isolated_chat, monkeypatch):
    from backend.api.chat import chat as endpoint
    from backend.schemas import ChatRequest
    barrier = threading.Barrier(2)
    original = chat.build_reply
    def synchronized(user, message):
        result = original(user, message)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(chat, 'build_reply', synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(endpoint, ChatRequest(message=message), user={'id': 'user'}) for message in ('first','second')]
        results = [f.result(timeout=10) for f in futures]
    for message, result in zip(('first','second'), results):
        assert result.reply == 'answer:' + message
        assert result.trace[-1]['length'] == len(result.reply)


def test_asgi_parallel_requests_keep_trace_and_leave_callers_result_untouched(isolated_chat, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.chat import chat_router, current_user
    chat.build_reply('outer-user', 'outer')
    outer = chat.last_summary()
    barrier = threading.Barrier(2)
    original = chat.build_reply
    def synchronized(user, message):
        result = original(user, message)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(chat, 'build_reply', synchronized)
    app = FastAPI()
    app.dependency_overrides[current_user] = lambda: {'id': 'shared-user'}
    app.include_router(chat_router, prefix='/api')
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.post, '/api/chat', json={'message':message}) for message in ('first','second')]
        results = [future.result(timeout=10) for future in futures]
    for message, response in zip(('first','second'), results):
        assert response.status_code == 200
        result = response.json()
        assert result['reply'] == 'answer:' + message
        assert result['suggest_ticket'] == (message == 'first')
        assert result['trace'][-1]['length'] == len(result['reply'])
    assert chat.last_summary() is outer
    assert chat.last_escalation().marker == 'outer'


def test_http_exception_restores_outer_context(isolated_chat, monkeypatch):
    from backend.api.chat import chat as endpoint
    from backend.schemas import ChatRequest
    chat.build_reply('outer-user', 'outer')
    outer = chat.last_summary()
    def fail(*args): raise RuntimeError('store unavailable')
    monkeypatch.setattr(chat, 'get_or_create_conversation', fail)
    with pytest.raises(RuntimeError): endpoint(ChatRequest(message='failed'), user={'id': 'user'})
    assert chat.last_summary() is outer
    assert chat.last_escalation().marker == 'outer'


def test_http_does_not_reuse_outer_summary_when_call_publishes_nothing(isolated_chat, monkeypatch):
    from backend.api.chat import chat as endpoint
    from backend.schemas import ChatRequest
    chat.build_reply('user', 'outer')
    monkeypatch.setattr(chat, 'build_reply', lambda *args: ('no summary', [], False))
    assert endpoint(ChatRequest(message='first'), user={'id': 'user'}).trace == []


def test_reused_thread_has_no_previous_request_state(isolated_chat):
    def execute(message):
        assert chat.last_summary() is None and chat.last_escalation() is None
        with chat.execution_scope():
            chat.build_reply('same-user', message)
            assert chat.last_summary().answer == 'answer:' + message
        assert chat.last_summary() is None and chat.last_escalation() is None
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(execute, 'first').result(timeout=5)
        pool.submit(execute, 'second').result(timeout=5)


def test_persistence_failure_after_agent_completion_publishes_no_old_result(isolated_chat, monkeypatch):
    chat.build_reply('user', 'first')
    def fail(*args): raise RuntimeError('store unavailable')
    monkeypatch.setattr(chat, 'append_turn', fail)
    with pytest.raises(RuntimeError): chat.build_reply('user', 'second')
    assert chat.last_summary() is None and chat.last_escalation() is None
