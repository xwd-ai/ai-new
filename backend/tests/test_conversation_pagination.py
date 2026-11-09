"""历史分页使用隔离 SQLite，兼容完整回放并保持用户边界。"""

import pytest
import sqlalchemy as sa

from backend.api import chat as api
from backend.models import Conversation, Message
from backend.tests.test_conversation_selection import selection_store


@pytest.fixture()
def history(selection_store):
    factory, client = selection_store
    with factory() as db:
        for key, user, updated in [('active', 'user', 100), ('old', 'user', 50), ('private', 'other', 200)]:
            db.add(Conversation(id=key, user_id=user, title='', created_at=1, updated_at=updated))
        for i in reversed(range(105)):
            db.add(Message(id=f'm-{i:03}', conversation_id='active', role='user', content=str(i), at=1))
        for key in ('old', 'private'):
            db.add(Message(id=key, conversation_id=key, role='user', content=key, at=1))
        db.commit()
    return factory, client


def test_legacy_and_paged_history_keep_stable_order_and_count(history):
    _, client = history
    legacy = client.get('/api/conversation').json()
    assert set(legacy) == {'messages'} and len(legacy['messages']) == 105
    assert [m['content'] for m in legacy['messages']] == list(map(str, range(105)))
    for params, expected in [({'limit': 2}, ['0', '1']), ({'offset': 1, 'limit': 2}, ['1', '2']),
                             ({'offset': 100}, list(map(str, range(100, 105))))]:
        response = client.get('/api/conversation', params=params)
        assert response.status_code == 200
        assert response.json()['total'] == 105
        assert [m['content'] for m in response.json()['messages']] == expected
    assert len(client.get('/api/conversation', params={'offset': 0}).json()['messages']) == 100
    for offset in (105, 2**63):
        assert client.get('/api/conversation', params={'offset': offset}).json() == {'messages': [], 'total': 105}


@pytest.mark.parametrize('params', [{'offset': -1}, {'limit': 0}, {'limit': 101}, {'limit': 'bad'}])
def test_invalid_history_paging_returns_422(selection_store, params):
    _, client = selection_store
    assert client.get('/api/conversation', params=params).status_code == 422


def test_missing_conversation_and_anonymous_have_no_total_leak(history):
    _, client = history
    for identity in (None, {'id': 'empty-user'}):
        client.app.dependency_overrides[api.current_user] = lambda: identity
        assert client.get('/api/conversation').json() == {'messages': []}
        assert client.get('/api/conversation', params={'limit': 1}).json() == {'messages': [], 'total': 0}


def test_existing_empty_conversation_returns_zero(selection_store):
    factory, client = selection_store
    with factory() as db:
        db.add(Conversation(id='empty', user_id='user', title='', created_at=1, updated_at=1))
        db.commit()
    assert client.get('/api/conversation').json() == {'messages': []}
    assert client.get('/api/conversation', params={'offset': 1}).json() == {'messages': [], 'total': 0}


def test_count_and_window_share_snapshot(history):
    factory, client = history
    engine = factory.kw['bind']
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL').close()
    inserted = False

    @sa.event.listens_for(engine, 'after_cursor_execute')
    def append_after_count(connection, cursor, statement, parameters, context, executemany):
        nonlocal inserted
        if not inserted and 'count(' in statement.lower():
            inserted = True
            with engine.begin() as writer:
                writer.execute(sa.insert(Message).values(id='new', conversation_id='active', role='user', content='new', at=0))

    before = client.get('/api/conversation', params={'limit': 1}).json()
    assert inserted and before['total'] == 105 and before['messages'][0]['content'] == '0'
    after = client.get('/api/conversation', params={'limit': 1}).json()
    assert after['total'] == 106 and after['messages'][0]['content'] == 'new'
