"""待恢复记录分页使用隔离数据库和真实 API 路由。"""

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.api import knowledge as api
from backend.database import Base
from backend.models import KnowledgeIngestJournal

PENDING = '/api/knowledge/ingest/pending'
ADMIN = {'Authorization': 'admin'}


@pytest.fixture()
def listing(tmp_path, monkeypatch):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'pending.db'}")
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL').close()
    Base.metadata.create_all(engine)
    monkeypatch.setattr(api.ingest_service, 'SessionLocal', sessionmaker(bind=engine))
    monkeypatch.setattr(api.chat_service, 'current_user', lambda token: {'role': token})
    app = FastAPI()
    app.include_router(api.knowledge_router, prefix='/api')
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def insert(engine, ids, *, stage='chunked', updated_at=2):
    with engine.begin() as connection:
        connection.execute(sa.insert(KnowledgeIngestJournal), [
            {'fingerprint': key, 'knowledge_id': f'k-{key}', 'payload': {'content': '暂存正文'},
             'stage': stage, 'chunk_count': 1, 'updated_at': updated_at}
            for key in ids
        ])


def test_legacy_and_paged_reads_beyond_first_hundred(listing):
    client, engine = listing
    ids = [f'{i:03}' for i in range(105)]
    insert(engine, reversed(ids))
    insert(engine, ['finished'], stage='stored', updated_at=0)
    insert(engine, ['oldest'], updated_at=1)
    statements = []

    @sa.event.listens_for(engine, 'before_cursor_execute')
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith('select'):
            statements.append(statement.lower())

    expected = ['oldest', *ids]
    legacy = client.get(PENDING, headers=ADMIN).json()
    assert set(legacy) == {'items'}
    assert [row['fingerprint'] for row in legacy['items']] == expected[:100]
    page = client.get(PENDING, params={'offset': 100, 'limit': 3}, headers=ADMIN).json()
    assert page['total'] == 106
    assert [row['fingerprint'] for row in page['items']] == expected[100:103]
    assert set(page['items'][0]) == {'fingerprint', 'knowledge_id', 'stage', 'chunk_count', 'updated_at'}
    remainder = client.get(PENDING, params={'offset': 100}, headers=ADMIN).json()
    assert [row['fingerprint'] for row in remainder['items']] == expected[100:]
    filtered = client.get(PENDING, params={'stage': 'chunked'}, headers=ADMIN).json()
    assert filtered == {'items': legacy['items'], 'total': 106}
    assert statements and all('payload' not in statement for statement in statements)


@pytest.mark.parametrize('params', [{'offset': -1}, {'limit': 0}, {'limit': 101}, {'limit': 'bad'}])
def test_invalid_parameters_return_422(listing, params):
    client, _ = listing
    assert client.get(PENDING, params=params, headers=ADMIN).status_code == 422


def test_empty_pages_keep_total_and_require_admin(listing):
    client, engine = listing
    assert client.get(PENDING, params={'limit': 1}, headers=ADMIN).json() == {'items': [], 'total': 0}
    insert(engine, ['one'])
    for offset in (1, 2**63):
        assert client.get(PENDING, params={'offset': offset}, headers=ADMIN).json() == {'items': [], 'total': 1}
    assert client.get(PENDING, params={'offset': 0}, headers={'Authorization': 'customer'}).status_code == 403


def test_stage_filter_applies_to_items_total_and_offset(listing):
    client, engine = listing
    insert(engine, ['a', 'd'], stage='parsed')
    insert(engine, ['b', 'c'], stage='chunked')
    insert(engine, ['finished'], stage='stored', updated_at=0)

    parsed = client.get(PENDING, params={'stage': 'parsed', 'offset': 1, 'limit': 1}, headers=ADMIN).json()
    assert parsed['total'] == 2
    assert [row['fingerprint'] for row in parsed['items']] == ['d']
    chunked = client.get(PENDING, params={'stage': 'chunked'}, headers=ADMIN).json()
    assert chunked['total'] == 2
    assert [row['fingerprint'] for row in chunked['items']] == ['b', 'c']
    assert client.get(PENDING, params={'stage': 'parsed', 'offset': 2}, headers=ADMIN).json() == {
        'items': [], 'total': 2,
    }
    assert client.get(PENDING, params={'stage': 'parsed'}, headers={'Authorization': 'customer'}).status_code == 403


@pytest.mark.parametrize('stage', ['stored', 'unknown', ''])
def test_invalid_stage_returns_422(listing, stage):
    client, _ = listing
    assert client.get(PENDING, params={'stage': stage}, headers=ADMIN).status_code == 422


def test_empty_stage_filter_excludes_other_pending_records(listing):
    client, engine = listing
    insert(engine, ['one'], stage='chunked')
    assert client.get(PENDING, params={'stage': 'parsed'}, headers=ADMIN).json() == {'items': [], 'total': 0}


@pytest.mark.parametrize('stage', [None, 'chunked'])
def test_count_and_page_share_snapshot_during_recovery(listing, stage):
    client, engine = listing
    insert(engine, ['a', 'b'])
    params = {'limit': 1}
    if stage is not None:
        insert(engine, ['older-other-stage'], stage='parsed', updated_at=0)
        params['stage'] = stage
    updated = False

    @sa.event.listens_for(engine, 'after_cursor_execute')
    def recover_first(connection, cursor, statement, parameters, context, executemany):
        nonlocal updated
        if not updated and 'count(' in statement.lower():
            updated = True
            with engine.begin() as writer:
                writer.execute(sa.update(KnowledgeIngestJournal).where(
                    KnowledgeIngestJournal.fingerprint == 'a').values(stage='stored'))

    before = client.get(PENDING, params=params, headers=ADMIN).json()
    assert updated and before['total'] == 2 and before['items'][0]['fingerprint'] == 'a'
    after = client.get(PENDING, params=params, headers=ADMIN).json()
    assert after['total'] == 1 and after['items'][0]['fingerprint'] == 'b'
