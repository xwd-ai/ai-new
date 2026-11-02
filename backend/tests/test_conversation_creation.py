import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models import Conversation
from backend.services import chat


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / 'conversation.db'
    engine = sa.create_engine(f'sqlite:///{path}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    class TestSession(Session):
        pass
    factory = sessionmaker(bind=engine, class_=TestSession, autoflush=False)
    monkeypatch.setattr(chat, 'SessionLocal', factory)
    yield path, engine, factory, TestSession
    engine.dispose()


def test_simultaneous_empty_reads_in_threads_create_one_conversation(store):
    _, engine, factory, _ = store
    barrier = threading.Barrier(2)
    seen = threading.local()
    @sa.event.listens_for(engine, 'after_cursor_execute')
    def pause_first_read(conn, cursor, statement, parameters, context, executemany):
        if 'FROM conversations' in statement and not getattr(seen, 'read', False):
            seen.read = True
            barrier.wait(timeout=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(chat.get_or_create_conversation, 'shared-user') for _ in range(2)]
        ids = [f.result(timeout=20) for f in futures]
    sa.event.remove(engine, 'after_cursor_execute', pause_first_read)
    assert ids[0] == ids[1]
    with factory() as db:
        assert db.query(Conversation).count() == 1


def test_simultaneous_empty_reads_across_processes_create_one_conversation(store, tmp_path):
    path, _, factory, _ = store
    processes = []
    try:
        for worker in ('first', 'second'):
            processes.append(subprocess.Popen(
                [sys.executable, '-m', 'backend.tests.conversation_create_worker', str(path), str(tmp_path), worker],
                cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8',
            ))
        deadline = time.monotonic() + 15
        while not all((tmp_path / f'{worker}.ready').exists() for worker in ('first', 'second')):
            assert all(p.poll() is None for p in processes), 'worker exited before initial read'
            assert time.monotonic() < deadline, 'workers did not reach initial reads'
            time.sleep(0.02)
        (tmp_path / 'go').write_text('go')
        ids = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            ids.append(json.loads(stdout))
        assert ids[0] == ids[1]
        with factory() as db:
            assert db.query(Conversation).count() == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


def test_existing_multiple_conversations_keep_latest_and_other_user_separate(store):
    _, _, factory, _ = store
    with factory() as db:
        db.add_all([Conversation(id=key, user_id='user', title=key, created_at=1, updated_at=at)
                    for key, at in [('old', 2), ('latest', 3)]])
        db.commit()
    assert chat.get_or_create_conversation('user') == 'latest'
    other = chat.get_or_create_conversation('other')
    assert other not in ('old', 'latest')
    with factory() as db:
        assert db.query(Conversation).count() == 3
        assert db.get(Conversation, 'latest').updated_at == 3
        assert db.get(Conversation, other).user_id == 'other'


def test_creation_failure_rolls_back_and_releases_lock_for_retry(store):
    _, _, factory, session_class = store
    def fail_commit(db):
        db.flush()
        raise RuntimeError('injected failure')
    sa.event.listen(session_class, 'before_commit', fail_commit)
    try:
        with pytest.raises(RuntimeError, match='injected failure'):
            chat.get_or_create_conversation('user')
    finally:
        sa.event.remove(session_class, 'before_commit', fail_commit)
    with factory() as db:
        assert db.query(Conversation).count() == 0
    created = chat.get_or_create_conversation('user')
    assert chat.get_or_create_conversation('user') == created


def test_busy_database_fails_without_duplicate_and_can_retry_after_release(store, monkeypatch):
    path, engine, factory, _ = store
    contender = sa.create_engine(f'sqlite:///{path}', connect_args={'timeout': 0.02})
    monkeypatch.setattr(chat, 'SessionLocal', sessionmaker(bind=contender, autoflush=False))
    try:
        with engine.connect() as holder:
            holder.exec_driver_sql('BEGIN IMMEDIATE')
            with pytest.raises(sa.exc.OperationalError, match='locked'):
                chat.get_or_create_conversation('user')
            holder.rollback()
        with factory() as db:
            assert db.query(Conversation).count() == 0
        created = chat.get_or_create_conversation('user')
        assert chat.get_or_create_conversation('user') == created
    finally:
        contender.dispose()
