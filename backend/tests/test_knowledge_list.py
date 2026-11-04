"""在隔离 SQLite 中验证摘要投影、分页和权限。"""

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.api import knowledge as api
from backend.database import Base
from backend.models import KnowledgeDoc


@pytest.fixture()
def listing(tmp_path, monkeypatch):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'listing.db'}")
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA journal_mode=WAL').close()
    Base.metadata.create_all(engine)
    monkeypatch.setattr(api.knowledge, 'SessionLocal', sessionmaker(bind=engine))
    monkeypatch.setattr(api.chat_service, 'current_user', lambda token: {'role': token} if token else None)
    app = FastAPI()
    app.include_router(api.knowledge_router, prefix='/api')
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def insert_docs(engine, ids):
    with engine.begin() as conn:
        conn.execute(sa.insert(KnowledgeDoc), [
            {'id': key, 'title': key, 'kind': 'faq', 'content': '正文不应被查询', 'keywords': ['secret']}
            for key in ids
        ])


@pytest.mark.parametrize('params, expected', [
    ({}, ['a', 'b', 'c']), ({'limit': 1}, ['a']),
    ({'offset': 1, 'limit': 1}, ['b']), ({'offset': 1}, ['b', 'c']),
    ({'offset': 3, 'limit': 1}, []), ({'offset': 2**63, 'limit': 1}, []),
])
def test_list_returns_ordered_summaries_and_full_count(listing, params, expected):
    client, engine = listing
    insert_docs(engine, ['c', 'a', 'b'])
    statements = []
    @sa.event.listens_for(engine, 'before_cursor_execute')
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith('select'):
            statements.append(statement.lower())
    response = client.get('/api/knowledge/items', params=params, headers={'Authorization': 'admin'})
    assert response.status_code == 200
    assert response.json() == {'total': 3, 'items': [
        {'id': key, 'title': key, 'kind': 'faq'} for key in expected
    ]}
    assert statements and all('content' not in sql and 'keywords' not in sql for sql in statements)


@pytest.mark.parametrize('params', [{'limit': 0}, {'limit': 101}, {'limit': 'bad'}, {'offset': -1}])
def test_invalid_pagination_returns_422(listing, params):
    client, _ = listing
    assert client.get('/api/knowledge/items', params=params, headers={'Authorization': 'admin'}).status_code == 422


def test_empty_page_and_permissions(listing):
    client, _ = listing
    url = '/api/knowledge/items?limit=2'
    assert client.get(url).status_code == 403
    assert client.get(url, headers={'Authorization': 'customer'}).status_code == 403
    assert client.get(url, headers={'Authorization': 'admin'}).json() == {'total': 0, 'items': []}


def test_count_and_page_share_snapshot_during_ingest(listing):
    client, engine = listing
    insert_docs(engine, ['b', 'c'])
    inserted = False
    @sa.event.listens_for(engine, 'after_cursor_execute')
    def concurrent_insert(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserted
        if not inserted and 'count(' in statement.lower():
            inserted = True
            insert_docs(engine, ['a'])
    response = client.get('/api/knowledge/items?limit=1', headers={'Authorization': 'admin'})
    assert inserted and response.status_code == 200
    assert response.json() == {'total': 2, 'items': [{'id': 'b', 'title': 'b', 'kind': 'faq'}]}
    next_response = client.get('/api/knowledge/items?limit=1', headers={'Authorization': 'admin'})
    assert next_response.json() == {'total': 3, 'items': [{'id': 'a', 'title': 'a', 'kind': 'faq'}]}


def insert_filter_docs(engine):
    with engine.begin() as conn:
        conn.execute(sa.insert(KnowledgeDoc), [
            {'id': key, 'title': title, 'kind': kind, 'content': '导出仅在正文中', 'keywords': ['secret']}
            for key, title, kind in [
                ('d', '导出规则', 'rule'), ('b', '导出 FAQ', 'faq'),
                ('a', '导出手册', 'manual'), ('c', '其他 FAQ', 'faq'),
                ('e', '100%_完成/路径', 'manual'), ('f', '100XX完成X路径', 'manual'),
            ]
        ])


@pytest.mark.parametrize('params, total, expected', [
    ({'title': '导出'}, 3, ['a', 'b', 'd']),
    ({'kind': 'faq'}, 2, ['b', 'c']),
    ({'title': '导出', 'kind': 'faq'}, 1, ['b']),
    ({'title': '导出', 'offset': 1, 'limit': 1}, 3, ['b']),
    ({'title': '导出', 'offset': 1}, 3, ['b', 'd']),
    ({'kind': 'faq', 'offset': 2}, 2, []),
    ({'title': '导出', 'offset': 2**63, 'limit': 1}, 3, []),
    ({'title': '%_'}, 1, ['e']),
    ({'title': '/路径'}, 1, ['e']),
    ({'title': "' OR 1=1 --"}, 0, []),
    ({'title': '  导出  ', 'kind': 'rule'}, 1, ['d']),
    ({'title': '   ', 'kind': 'faq'}, 2, ['b', 'c']),
    ({'title': '', 'kind': 'faq'}, 2, ['b', 'c']),
    ({'title': 'faq', 'kind': 'faq', 'limit': 1}, 2, ['b']),
    ({'title': 'x' * 200}, 0, []),
])
def test_filters_apply_before_pagination_without_loading_bodies(listing, params, total, expected):
    client, engine = listing
    insert_filter_docs(engine)
    statements = []
    @sa.event.listens_for(engine, 'before_cursor_execute')
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith('select'):
            statements.append(statement.lower())
    response = client.get('/api/knowledge/items', params=params, headers={'Authorization': 'admin'})
    assert response.status_code == 200
    assert response.json()['total'] == total
    assert [item['id'] for item in response.json()['items']] == expected
    assert statements and all('content' not in sql and 'keywords' not in sql for sql in statements)
    for token in ('', 'customer'):
        assert client.get('/api/knowledge/items', params=params, headers={'Authorization': token}).status_code == 403


@pytest.mark.parametrize('params', [
    {'kind': 'unknown'}, {'kind': ''}, {'kind': 'FAQ'}, {'title': 'x' * 201},
])
def test_invalid_filters_return_422(listing, params):
    client, _ = listing
    assert client.get('/api/knowledge/items', params=params, headers={'Authorization': 'admin'}).status_code == 422


def test_filtered_count_and_page_share_snapshot(listing):
    client, engine = listing
    insert_filter_docs(engine)
    inserted = False
    @sa.event.listens_for(engine, 'after_cursor_execute')
    def concurrent_insert(conn, cursor, statement, parameters, context, executemany):
        nonlocal inserted
        if not inserted and 'count(' in statement.lower():
            inserted = True
            with engine.begin() as writer:
                writer.execute(sa.insert(KnowledgeDoc), {
                    'id': '0', 'title': '导出新增 FAQ', 'kind': 'faq', 'content': '新正文', 'keywords': [],
                })
    params = {'title': '导出', 'kind': 'faq', 'limit': 1}
    response = client.get('/api/knowledge/items', params=params, headers={'Authorization': 'admin'})
    assert inserted and response.status_code == 200
    assert response.json() == {'total': 1, 'items': [{'id': 'b', 'title': '导出 FAQ', 'kind': 'faq'}]}
    next_response = client.get('/api/knowledge/items', params=params, headers={'Authorization': 'admin'})
    assert next_response.json() == {'total': 2, 'items': [{'id': '0', 'title': '导出新增 FAQ', 'kind': 'faq'}]}
