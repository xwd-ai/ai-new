"""最近会话选择规则通过临时 SQLite 和真实 HTTP 路由验证。"""

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.tests.reference_api import chat as chat_api
from backend.database import Base
from backend.models import Conversation, Message
from backend.services import chat


@pytest.fixture()
def selection_store(tmp_path, monkeypatch):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'selection.db'}", connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(chat, 'SessionLocal', factory)
    monkeypatch.setattr(chat_api, 'SessionLocal', factory)
    app = FastAPI()
    app.dependency_overrides[chat_api.current_user] = lambda: {'id': 'user'}
    app.include_router(chat_api.chat_router, prefix='/api')
    with chat.execution_scope(), TestClient(app) as client:
        yield factory, client
    engine.dispose()


def populate(factory, rows):
    with factory() as db:
        for key, created, updated in rows:
            db.add(Conversation(id=key, user_id='user', title='', created_at=created, updated_at=updated))
            db.add(Message(id='msg-' + key, conversation_id=key, role='user', content=key, at=updated))
        # 其他用户的时间和编号均更大，也不得参与选择。
        db.add(Conversation(id='zz-private', user_id='other', title='', created_at=999, updated_at=999))
        db.add(Message(id='private', conversation_id='zz-private', role='user', content='private', at=999))
        db.commit()


@pytest.mark.parametrize('reader', ['service', 'http'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('rows, expected', [
    ([('a', 1, 100), ('z', 99, 90)], 'a'),
    ([('z', 1, 100), ('a', 2, 100)], 'a'),
    ([('a', 2, 100), ('z', 2, 100)], 'z'),
])
def test_latest_selection_uses_updated_created_then_id(selection_store, reader, reverse, rows, expected):
    factory, client = selection_store
    populate(factory, list(reversed(rows)) if reverse else rows)
    for _ in range(2):
        if reader == 'service':
            assert chat.get_or_create_conversation('user') == expected
        else:
            response = client.get('/api/conversation')
            assert response.status_code == 200
            assert [m['content'] for m in response.json()['messages']] == [expected]
    with factory() as db:
        assert db.query(Conversation).count() == 3
        assert sorted((c.id, c.created_at, c.updated_at) for c in db.query(Conversation)
                      .filter(Conversation.user_id == 'user')) == sorted(rows)


def test_reply_and_history_use_same_tied_conversation(selection_store, monkeypatch):
    factory, client = selection_store
    populate(factory, [('a', 2, 100), ('z', 2, 100)])
    monkeypatch.setattr(chat.orders, 'query_order', lambda _: None)
    monkeypatch.setattr(chat.knowledge, 'search_knowledge', lambda _: [])
    seen_history = []
    def step(message, **kwargs):
        seen_history.extend(kwargs['history'])
        return 'saved answer'
    monkeypatch.setattr(chat.llm, 'chat_step', step)
    response = client.post('/api/chat', json={'message': 'new question'})
    assert response.status_code == 200 and response.json()['reply'] == 'saved answer'
    assert seen_history == [{'role': 'user', 'content': 'z'}]
    assert [m['content'] for m in client.get('/api/conversation').json()['messages']] == [
        'z', 'new question', 'saved answer',
    ]
    with factory() as db:
        assert [(m.role, m.content) for m in db.query(Message)
                .filter(Message.conversation_id == 'a')] == [('user', 'a')]
        assert db.query(Message).filter(Message.conversation_id == 'z').count() == 3
