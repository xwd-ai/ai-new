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
    legacy_response = client.get('/api/conversation')
    assert legacy_response.headers['cache-control'] == 'no-store'
    legacy = legacy_response.json()
    assert set(legacy) == {'messages'} and len(legacy['messages']) == 105
    assert [m['content'] for m in legacy['messages']] == list(map(str, range(105)))
    for params, expected in [({'limit': 2}, ['0', '1']), ({'offset': 1, 'limit': 2}, ['1', '2']),
                             ({'offset': 100}, list(map(str, range(100, 105))))]:
        response = client.get('/api/conversation', params=params)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['total'] == 105
        assert [m['content'] for m in response.json()['messages']] == expected
    assert len(client.get('/api/conversation', params={'offset': 0}).json()['messages']) == 100
    for offset in (105, 2**63):
        page = client.get('/api/conversation', params={'offset': offset}).json()
        assert page['messages'] == []
        assert page['total'] == 105
        assert page['conversation_id'] == 'active'


def test_history_revision_is_shared_across_pages_and_changes_for_equal_count_edit(history):
    factory, client = history
    first = client.get('/api/conversation', params={'limit': 100}).json()
    with factory() as db:
        db.query(Message).filter_by(id='private').update({'content': 'edited elsewhere'})
        db.commit()
    second = client.get('/api/conversation', params={'offset': 100}).json()
    assert first['history_revision'] == second['history_revision']
    with factory() as db:
        db.query(Message).filter_by(id='m-000').update({'content': 'edited in the first page'})
        db.commit()
    after = client.get('/api/conversation', params={'offset': 100}).json()
    assert after['messages'] == second['messages']
    assert after['total'] == second['total']
    assert after['conversation_id'] == second['conversation_id']
    assert after['history_revision'] != second['history_revision']


@pytest.mark.parametrize('same_connection', [True, False])
def test_stable_pages_reuse_revision_but_committed_edits_invalidate_it(history, same_connection):
    factory, client = history
    engine = factory.kw['bind']
    scans = []

    @sa.event.listens_for(engine, 'before_cursor_execute')
    def record_scan(connection, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if sql.startswith('select') and 'from messages' in sql and 'limit' not in sql:
            scans.append(connection.connection.driver_connection)

    first = client.get('/api/conversation', params={'limit': 100}).json()
    second = client.get('/api/conversation', params={'offset': 100}).json()
    assert second['history_revision'] == first['history_revision']
    assert len(scans) == 1
    writer_engine = engine if same_connection else sa.create_engine(engine.url)
    try:
        with writer_engine.begin() as writer:
            assert (writer.connection.driver_connection is scans[0]) == same_connection
            writer.execute(sa.update(Message).where(Message.id == 'm-000').values(content='new'))
    finally:
        if not same_connection:
            writer_engine.dispose()
    after = client.get('/api/conversation', params={'offset': 100}).json()
    assert len(scans) == 2
    assert after['history_revision'] != first['history_revision']
    assert after['total'] == second['total']
    assert after['messages'] == second['messages']


@pytest.mark.parametrize('replace_connection', [False, True])
def test_history_revision_rebuilds_after_rollback_or_connection_replacement(history, replace_connection):
    factory, client = history
    engine = factory.kw['bind']
    scans = []

    @sa.event.listens_for(engine, 'before_cursor_execute')
    def record_scan(connection, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if sql.startswith('select') and 'from messages' in sql and 'limit' not in sql:
            scans.append(connection.connection.driver_connection)

    first = client.get('/api/conversation', params={'limit': 1}).json()
    assert len(scans) == 1
    if replace_connection:
        engine.dispose()
    else:
        with engine.connect() as writer:
            assert writer.connection.driver_connection is scans[0]
            writer.execute(sa.update(Message).where(Message.id == 'm-000').values(content='rolled back'))
            writer.rollback()

    next_page = client.get('/api/conversation', params={'limit': 1}).json()
    assert next_page == first
    assert len(scans) == 2
    assert (scans[1] is not scans[0]) == replace_connection
    assert client.get('/api/conversation', params={'offset': 100}).json()['history_revision'] == first['history_revision']
    assert len(scans) == 2


def test_equal_counts_still_identify_a_change_in_latest_conversation(history):
    factory, client = history
    first = client.get('/api/conversation', params={'limit': 100}).json()
    with factory() as db:
        db.query(Conversation).filter_by(id='old').update({'updated_at': 150})
        db.add_all([
            Message(id=f'old-{i:03}', conversation_id='old', role='user', content=f'old-{i}', at=2)
            for i in range(104)
        ])
        db.commit()
    second = client.get('/api/conversation', params={'offset': 100}).json()
    assert first['total'] == second['total'] == 105
    assert (first['conversation_id'], second['conversation_id']) == ('active', 'old')


@pytest.mark.parametrize('params', [{'offset': -1}, {'limit': 0}, {'limit': 101}, {'limit': 'bad'}])
def test_invalid_history_paging_returns_422(selection_store, params):
    _, client = selection_store
    assert client.get('/api/conversation', params=params).status_code == 422


def test_missing_conversation_and_anonymous_have_no_total_leak(history):
    _, client = history
    for identity in (None, {'id': 'empty-user'}):
        client.app.dependency_overrides[api.current_user] = lambda: identity
        assert client.get('/api/conversation').json() == {'messages': []}
        response = client.get('/api/conversation', params={'limit': 1})
        assert response.headers['cache-control'] == 'no-store'
        assert response.json() == {'messages': [], 'total': 0}


def test_existing_empty_conversation_returns_zero(selection_store):
    factory, client = selection_store
    with factory() as db:
        db.add(Conversation(id='empty', user_id='user', title='', created_at=1, updated_at=1))
        db.commit()
    assert client.get('/api/conversation').json() == {'messages': []}
    page = client.get('/api/conversation', params={'offset': 1}).json()
    assert page['messages'] == []
    assert page['total'] == 0
    assert page['conversation_id'] == 'empty'
    assert page['history_revision']


@pytest.mark.parametrize('warm_cache', [False, True])
def test_revision_count_and_window_share_snapshot(history, warm_cache):
    factory, client = history
    engine = factory.kw['bind']
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL').close()
    cached = client.get('/api/conversation', params={'limit': 1}).json() if warm_cache else None
    inserted = False

    @sa.event.listens_for(engine, 'after_cursor_execute')
    def append_during_read(connection, cursor, statement, parameters, context, executemany):
        nonlocal inserted
        if not inserted and 'from messages' in statement.lower():
            inserted = True
            with engine.begin() as writer:
                writer.execute(sa.insert(Message).values(id='new', conversation_id='active', role='user', content='new', at=0))

    before = client.get('/api/conversation', params={'limit': 1}).json()
    assert inserted and before['total'] == 105 and before['messages'][0]['content'] == '0'
    if warm_cache:
        assert before == cached
    after = client.get('/api/conversation', params={'limit': 1}).json()
    assert after['total'] == 106 and after['messages'][0]['content'] == 'new'
    assert before['history_revision'] != after['history_revision']
