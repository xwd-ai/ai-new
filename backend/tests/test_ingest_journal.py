import subprocess
import sys

import pytest
import sqlalchemy as sa

from backend.models import KnowledgeDoc, KnowledgeIngestJournal
from backend.services import ingest, ingest_leases
from backend.tests.test_ingest_leases import ingest_env
from backend.tests.test_api_contracts import api_client, _headers


def test_failed_chunking_retains_recoverable_input(ingest_env, monkeypatch):
    factory, _ = ingest_env
    original = ingest.split_into_chunks
    monkeypatch.setattr(ingest, 'split_into_chunks', lambda text: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError):
        ingest.ingest(title='恢复资料', content='待恢复正文', kind='faq', keywords=['恢复'])
    pending = ingest.pending_ingests()
    assert pending[0]['stage'] == 'parsed'
    assert 'payload' not in pending[0]
    monkeypatch.setattr(ingest, 'split_into_chunks', original)
    result = ingest.recover_ingest(pending[0]['fingerprint'])
    assert result.created
    assert ingest.pending_ingests() == []
    with factory() as db:
        assert db.get(KnowledgeDoc, result.knowledge_id).keywords == ['恢复']


def test_chunked_resume_skips_completed_step_and_preserves_first_metadata(ingest_env, monkeypatch):
    original = ingest._journal_chunked
    def interrupt(*args):
        original(*args)
        raise RuntimeError('after chunk commit')
    monkeypatch.setattr(ingest, '_journal_chunked', interrupt)
    with pytest.raises(RuntimeError):
        ingest.ingest(title='FAQ', content='正文', kind='faq', knowledge_id='kept-id')
    monkeypatch.setattr(ingest, '_journal_chunked', original)
    monkeypatch.setattr(ingest, 'split_into_chunks', lambda text: pytest.fail('分块不应重跑'))
    result = ingest.ingest(title='FAQ', content='正文', kind='rule')
    assert result.knowledge_id == 'kept-id' and result.chunks == 1
    with ingest_env[0]() as db:
        assert db.get(KnowledgeDoc, 'kept-id').kind == 'faq'


def test_final_write_rolls_back_journal_completion_together(ingest_env):
    factory, _ = ingest_env
    with factory() as db:
        db.add(KnowledgeDoc(id='conflict', title='已有', content='已有', kind='manual', keywords=[]))
        db.commit()
    with pytest.raises(sa.exc.IntegrityError):
        ingest.ingest(title='新资料', content='新正文', knowledge_id='conflict')
    assert ingest.pending_ingests()[0]['stage'] == 'chunked'
    with pytest.raises(ValueError, match='编号已被其他资料占用'):
        ingest.recover_ingest(ingest.pending_ingests()[0]['fingerprint'])
    with factory() as db:
        assert db.query(KnowledgeDoc).count() == 1


def test_completion_clears_payload_and_matches_document(ingest_env):
    result = ingest.ingest(title='完成资料', content='正文')
    with ingest_env[0]() as db:
        row = db.get(KnowledgeIngestJournal, ingest.content_fingerprint('完成资料', '正文'))
        assert row.stage == 'stored' and row.payload == {}
        assert row.knowledge_id == result.knowledge_id
        assert db.get(KnowledgeDoc, row.knowledge_id).fingerprint == row.fingerprint


def test_corrupted_pending_input_is_not_replayed(ingest_env):
    with ingest_env[0]() as db:
        db.add(KnowledgeIngestJournal(fingerprint='a'*64, knowledge_id='corrupt',
               payload=dict(title='不匹配', content='正文', kind='faq', keywords=[]),
               stage='parsed', chunk_count=0, updated_at=1))
        db.commit()
    with pytest.raises(ValueError, match='指纹不匹配'):
        ingest.recover_ingest('a'*64)
    with ingest_env[0]() as db:
        assert db.query(KnowledgeDoc).count() == 0


def test_crashed_process_recovers_after_lease_expiry(ingest_env):
    factory, path = ingest_env
    worker = '''
import os, sys
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from backend.services import ingest
ingest.SessionLocal = sessionmaker(bind=sa.create_engine('sqlite:///' + sys.argv[1]))
ingest._checkpoint_path = lambda: __import__('pathlib').Path(sys.argv[1]).with_suffix('.json')
original = ingest._journal_chunked
def crash(*args):
    original(*args)
    os._exit(23)
ingest._journal_chunked = crash
ingest.ingest(title='进程资料', content='跨进程恢复', knowledge_id='process-id')
'''
    child = subprocess.run([sys.executable, '-c', worker, str(path)], capture_output=True, timeout=15)
    assert child.returncode == 23, child.stderr
    fingerprint = ingest.pending_ingests()[0]['fingerprint']
    with pytest.raises(ingest_leases.IngestBusyError):
        ingest.recover_ingest(fingerprint)
    with factory() as db:
        db.execute(sa.text('UPDATE knowledge_ingest_leases SET expires_at=0'))
        db.commit()
    recovering = '''
import sys
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from backend.services import ingest
ingest.SessionLocal = sessionmaker(bind=sa.create_engine('sqlite:///' + sys.argv[1]))
ingest._checkpoint_path = lambda: __import__('pathlib').Path(sys.argv[1]).with_suffix('.json')
assert ingest.recover_ingest(sys.argv[2]).knowledge_id == 'process-id'
'''
    recovered = subprocess.run([sys.executable, '-c', recovering, str(path), fingerprint], capture_output=True, timeout=15)
    assert recovered.returncode == 0, recovered.stderr
    assert not ingest.recover_ingest(fingerprint).created
    assert ingest.pending_ingests() == []


def test_recovery_api_permissions_and_missing_record(api_client):
    for headers in ({}, _headers(api_client, 'customer')):
        assert api_client.post('/api/knowledge/ingest/recover/' + 'a'*64, headers=headers).status_code == 403
    assert api_client.post('/api/knowledge/ingest/recover/' + 'a'*64, headers=_headers(api_client)).status_code == 404


def test_recovery_api_resumes_pending_and_handles_active_lease(api_client, monkeypatch):
    original = ingest.split_into_chunks
    monkeypatch.setattr(ingest, 'split_into_chunks', lambda _: (_ for _ in ()).throw(RuntimeError('interrupted')))
    with pytest.raises(RuntimeError):
        ingest.ingest(title='API资料', content='等待恢复')
    monkeypatch.setattr(ingest, 'split_into_chunks', original)
    headers = _headers(api_client)
    fingerprint = ingest.content_fingerprint('API资料', '等待恢复')
    with ingest_leases.hold(fingerprint, ingest.SessionLocal):
        busy = api_client.post('/api/knowledge/ingest/recover/' + fingerprint, headers=headers)
        assert busy.status_code == 409 and busy.headers['Retry-After'] == '1'
    assert api_client.post('/api/knowledge/ingest/recover/' + fingerprint, headers=headers).json()['created'] is True
    assert api_client.post('/api/knowledge/ingest/recover/' + fingerprint, headers=headers).json()['created'] is False
