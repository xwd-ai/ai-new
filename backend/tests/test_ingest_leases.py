"""入库租约使用临时 SQLite 验证，包含独立 Python 进程竞争。"""

from concurrent.futures import ThreadPoolExecutor
import importlib
import importlib.util
import json
import subprocess
import sys
import threading
import time

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend import database, models
from backend.api.knowledge import knowledge_router
from backend.services import chat, ingest


@pytest.fixture()
def ingest_env(tmp_path, monkeypatch):
    path = tmp_path / "ingest.db"
    engine = sa.create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(ingest, "SessionLocal", factory)
    monkeypatch.setattr(ingest, "_checkpoint_path", lambda: tmp_path / "checkpoints.json")
    yield factory, path
    engine.dispose()


def _leases():
    assert importlib.util.find_spec("backend.services.ingest_leases") is not None, "入库租约尚未实现"
    return importlib.import_module("backend.services.ingest_leases")


def _expire(factory, fingerprint):
    with factory() as db:
        db.execute(sa.text("UPDATE knowledge_ingest_leases SET expires_at=0 WHERE fingerprint=:fp"), {"fp": fingerprint})
        db.commit()


def test_same_document_cannot_be_written_by_two_concurrent_calls(ingest_env, monkeypatch):
    factory, _ = ingest_env
    entered, finish = threading.Event(), threading.Event()
    original = ingest.split_into_chunks

    def slow(content):
        if not entered.is_set():
            entered.set()
            assert finish.wait(10)
        return original(content)

    monkeypatch.setattr(ingest, "split_into_chunks", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(ingest.ingest, title="FAQ", content="并发资料")
        assert entered.wait(5)
        try:
            with pytest.raises(RuntimeError, match="处理中"):
                ingest.ingest(title="FAQ", content="并发资料")
        finally:
            finish.set()
        assert first.result(timeout=10).created
    with factory() as db:
        assert db.query(models.KnowledgeDoc).count() == 1


def test_expired_owner_cannot_renew_or_release_new_owners_lease(ingest_env):
    factory, _ = ingest_env
    leases = _leases()
    with leases.hold("same", factory) as first:
        _expire(factory, "same")
        with leases.hold("same", factory) as second:
            with factory() as db, pytest.raises(leases.IngestLeaseLostError):
                first.refresh(db)
            assert first.owner != second.owner
            first.release()
            with pytest.raises(leases.IngestBusyError):
                with leases.hold("same", factory):
                    pass


def test_heartbeat_keeps_lease_alive_past_initial_expiry(ingest_env, monkeypatch):
    factory, _ = ingest_env
    leases = _leases()
    monkeypatch.setattr(leases, "LEASE_SECONDS", 1.0)
    monkeypatch.setattr(leases, "HEARTBEAT_SECONDS", 0.05)
    with leases.hold("slow", factory):
        time.sleep(1.15)
        with pytest.raises(leases.IngestBusyError):
            with leases.hold("slow", factory):
                pass


def test_processing_failure_releases_lease_and_retry_can_succeed(ingest_env, monkeypatch):
    _leases()
    original = ingest.split_into_chunks

    def fail(_content):
        raise ValueError("分块失败")

    monkeypatch.setattr(ingest, "split_into_chunks", fail)
    with pytest.raises(ValueError, match="分块失败"):
        ingest.ingest(title="FAQ", content="资料")
    monkeypatch.setattr(ingest, "split_into_chunks", original)
    assert ingest.ingest(title="FAQ", content="资料").created


def test_invalid_checkpoint_metadata_does_not_block_ingest(ingest_env):
    _, path = ingest_env
    (path.parent / "checkpoints.json").write_text(
        json.dumps({"invalid": {"at": "broken"}}), encoding="utf-8",
    )
    assert ingest.ingest(title="FAQ", content="资料").created


def test_database_failure_rolls_back_and_releases_lease(ingest_env):
    ingest.ingest(title="FAQ", content="第一份资料", knowledge_id="fixed-id")
    with pytest.raises(sa.exc.IntegrityError):
        ingest.ingest(title="FAQ", content="第二份资料", knowledge_id="fixed-id")
    assert ingest.ingest(title="FAQ", content="第二份资料").created


def test_lost_lease_prevents_document_commit(ingest_env, monkeypatch):
    factory, _ = ingest_env
    leases = _leases()
    original = ingest.split_into_chunks

    def expire_during_work(content):
        _expire(factory, ingest.content_fingerprint("FAQ", content))
        return original(content)

    monkeypatch.setattr(ingest, "split_into_chunks", expire_during_work)
    with pytest.raises(leases.IngestLeaseLostError):
        ingest.ingest(title="FAQ", content="过期资料")
    with factory() as db:
        assert db.query(models.KnowledgeDoc).count() == 0


def test_final_transaction_rechecks_an_outdated_lookup(ingest_env, monkeypatch):
    factory, _ = ingest_env
    first = ingest.ingest(title="FAQ", content="已存在的资料")
    monkeypatch.setattr(ingest, "_find_existing", lambda _: None)
    repeated = ingest.ingest(title="FAQ", content="已存在的资料")
    assert not repeated.created
    assert repeated.knowledge_id == first.knowledge_id
    with factory() as db:
        assert db.query(models.KnowledgeDoc).count() == 1


def test_renewal_waiting_for_write_lock_cannot_revive_an_expired_lease(ingest_env, monkeypatch):
    factory, _ = ingest_env
    leases = _leases()
    monkeypatch.setattr(leases, "LEASE_SECONDS", 0.2)
    with leases.hold("blocked", factory) as lease:
        with factory() as blocker:
            blocker.execute(sa.text("UPDATE knowledge_ingest_leases SET owner=owner WHERE fingerprint='blocked'"))
            started = threading.Event()

            def refresh():
                with factory() as db:
                    started.set()
                    lease.refresh(db)
                    db.commit()

            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(refresh)
                assert started.wait(5)
                time.sleep(0.3)
                blocker.commit()
                with pytest.raises(leases.IngestLeaseLostError):
                    pending.result(timeout=5)


def test_busy_ingest_returns_409_with_retry_hint(ingest_env, monkeypatch):
    factory, _ = ingest_env
    leases = _leases()
    monkeypatch.setattr(chat, "current_user", lambda _: {"id": "admin", "role": "admin"})
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api")
    fingerprint = ingest.content_fingerprint("FAQ", "正在处理的资料")
    with leases.hold(fingerprint, factory), TestClient(app) as client:
        response = client.post("/api/knowledge/ingest", json={"title": "FAQ", "content": "正在处理的资料"})
    assert response.status_code == 409
    assert "稍后重试" in response.json()["detail"]
    assert response.headers.get("Retry-After")


WORKER = r'''
import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.services import ingest
engine = create_engine('sqlite:///' + Path(sys.argv[1]).as_posix())
ingest.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
ingest._checkpoint_path = lambda: Path(sys.argv[1]).with_suffix('.checkpoint.json')
if sys.argv[2] == 'holder':
    original = ingest.split_into_chunks
    def slow(content):
        print('processing', flush=True)
        sys.stdin.readline()
        return original(content)
    ingest.split_into_chunks = slow
try:
    result = ingest.ingest(title='FAQ', content='process document')
    print('created' if result.created else 'existing', flush=True)
except RuntimeError:
    print('busy', flush=True)
engine.dispose()
'''


def test_independent_processes_share_ingest_exclusion(ingest_env):
    _, path = ingest_env
    _leases()
    holder = subprocess.Popen([sys.executable, "-c", WORKER, str(path), "holder"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
    try:
        assert holder.stdout.readline().strip() == "processing"
        peer = subprocess.run([sys.executable, "-c", WORKER, str(path), "peer"],
                              capture_output=True, text=True, encoding="utf-8", timeout=15)
        assert peer.returncode == 0, peer.stderr
        assert peer.stdout.strip() == "busy"
        output, errors = holder.communicate("\n", timeout=15)
        assert holder.returncode == 0, errors
        assert output.strip() == "created"
        assert not ingest.ingest(title="FAQ", content="process document").created
    finally:
        if holder.poll() is None:
            holder.communicate("\n", timeout=15)
