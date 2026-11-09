"""真实本机 HTTP、独立进程与临时 SQLite；不调用外部模型。"""

import os
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest

from backend.tests.http_startup import failure_detail, read_port


ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name('http_e2e_worker.py')


def stop_server(process, base_url):
    if getattr(process, '_e2e_stopped', False):
        return
    if base_url:
        try:
            with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
                client.post('/__e2e_shutdown')
        except httpx.TransportError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        # Windows venv launcher 的子解释器也必须退出，不能只终止 launcher。
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           capture_output=True, timeout=10)
        else:
            process.kill()
        process.wait(timeout=5)
    if base_url:
        with httpx.Client(base_url=base_url, timeout=1, trust_env=False) as client:
            try:
                client.get('/health')
            except httpx.TransportError:
                process._e2e_stopped = True
                return
        raise RuntimeError('测试服务未退出，不能将此状态当作成功重启')
    process._e2e_stopped = True


@contextmanager
def server_pool(tmp_path):
    processes = []

    def start(mode='normal', *, timeout=25):
        run = tmp_path / str(len(processes))
        run.mkdir()
        output = (run / 'server.log').open('w', encoding='utf-8')
        process = subprocess.Popen(
            [sys.executable, str(WORKER), str(tmp_path), str(run), mode],
            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
            env={**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONIOENCODING': 'utf-8'},
        )
        entry = {'process': process, 'output': output, 'url': None}
        processes.append(entry)
        client = None
        last_probe = 'port_not_published'
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline and process.poll() is None:
            port = read_port(run)
            if port is not None:
                if client is None:
                    client = httpx.Client(base_url=f'http://127.0.0.1:{port}',
                                          timeout=5, trust_env=False)
                    entry['url'] = str(client.base_url)
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    response = client.get('/health', timeout=min(1, remaining))
                    last_probe = f'HTTP {response.status_code}'
                    if response.status_code == 200:
                        base_url = str(client.base_url)
                        client.close()
                        return process, httpx.Client(base_url=base_url, timeout=5, trust_env=False)
                except httpx.TransportError as error:
                    last_probe = type(error).__name__
            time.sleep(0.05)
        if client is not None:
            client.close()
        pytest.fail(failure_detail(process, run, started, entry['url'], last_probe))

    try:
        yield start, processes
    finally:
        errors = []
        for entry in processes:
            try:
                stop_server(entry['process'], entry['url'])
            except Exception as error:
                errors.append(str(error))
            finally:
                entry['output'].close()
        if errors:
            pytest.fail('测试服务清理失败：' + '; '.join(errors))


@pytest.fixture()
def server(tmp_path):
    with server_pool(tmp_path) as (start, _):
        yield start


def login(client, role):
    response = client.post('/api/login', json={'username': role, 'password': role + '123'})
    assert response.status_code == 200
    assert response.json()['token']
    return {'Authorization': 'Bearer ' + response.json()['token']}


DOCUMENT = {'title': 'E2E_GUIDE', 'content': 'e2eexport: choose CSV and check export permission.',
            'kind': 'faq', 'keywords': ['e2eexport']}


def verify_answer(client, headers, source_id):
    response = client.post('/api/chat', headers=headers, json={'message': 'e2eexport'})
    assert response.status_code == 200
    answer = response.json()
    assert 'choose CSV' in answer['reply'] and '[来源: E2E_GUIDE]' in answer['reply']
    assert '<ref' not in answer['reply'] and 'c999' not in answer['reply']
    assert [source['id'] for source in answer['sources']] == [source_id]
    assert answer['trace'][-1]['kind'] == 'done'
    return answer


def test_http_ingest_retrieval_chat_and_restart_persistence(server):
    process, client = server()
    with client:
        admin, customer = login(client, 'admin'), login(client, 'customer')
        denied = client.post('/api/knowledge/ingest', json=DOCUMENT, headers=customer)
        assert denied.status_code == 403
        created = client.post('/api/knowledge/ingest', json=DOCUMENT, headers=admin)
        assert created.status_code == 200 and created.json()['created']
        source_id = created.json()['knowledge_id']
        repeated = client.post('/api/knowledge/ingest', json=DOCUMENT, headers=admin).json()
        assert not repeated['created'] and repeated['knowledge_id'] == source_id
        assert client.get('/api/sources/' + source_id).json()['content'] == DOCUMENT['content']
        diagnostic = client.get('/api/knowledge/diagnose', params={'q':'e2eexport'}, headers=admin)
        assert source_id in diagnostic.json()['final_ids']
        answer = verify_answer(client, customer, source_id)
        before = client.get('/api/conversation', headers=customer).json()
        assert [m['role'] for m in before['messages']] == ['user', 'assistant']
        assert before['messages'][-1]['content'] == answer['reply']
        assert client.get('/api/conversation', headers=admin).json() == {'messages': []}
        stop_server(process, str(client.base_url))
    _, restarted = server()
    with restarted:
        # 同一持久化登录态、资料及历史必须跨进程保留。
        assert restarted.get('/api/me', headers=customer).json()['user']['role'] == 'customer'
        assert restarted.get('/api/conversation', headers=customer).json() == before
        assert restarted.get('/api/sources/' + source_id).json()['content'] == DOCUMENT['content']
        assert not restarted.post('/api/knowledge/ingest', headers=admin, json=DOCUMENT).json()['created']
        verify_answer(restarted, customer, source_id)


def test_http_optional_identity_logout_and_history_isolation(server):
    process, client = server()
    with client:
        admin, customer = login(client, 'admin'), login(client, 'customer')
        created = client.post('/api/knowledge/ingest', headers=admin, json=DOCUMENT)
        assert created.status_code == 200
        source_id = created.json()['knowledge_id']
        verify_answer(client, customer, source_id)
        before = client.get('/api/conversation', headers=customer).json()
        assert len(before['messages']) == 2
        page = client.get('/api/conversation', headers=customer, params={'offset': 1, 'limit': 1})
        assert page.status_code == 200
        assert page.json() == {'messages': before['messages'][1:2], 'total': 2}
        assert client.post('/api/logout', headers=customer).status_code == 200
        # 同一用户重新登录的凭据仍应能读取原来的私人历史。
        renewed = login(client, 'customer')
        assert renewed != customer
        for headers in ({}, {'Authorization': 'Bearer not-a-token'}, customer):
            assert client.get('/api/me', headers=headers).json() == {'user': None}
            assert client.get('/api/conversation', headers=headers).json() == {'messages': []}
            assert client.get('/api/conversation', headers=headers, params={'limit': 1}).json() == {'messages': [], 'total': 0}
            verify_answer(client, headers, source_id)
            assert client.get('/api/conversation', headers=headers).json() == {'messages': []}
            assert client.get('/api/conversation', headers=renewed).json() == before
            assert client.get('/api/conversation', headers=admin).json() == {'messages': []}
        stop_server(process, str(client.base_url))
    _, restarted = server()
    with restarted:
        assert restarted.get('/api/me', headers=customer).json() == {'user': None}
        assert restarted.get('/api/conversation', headers=customer).json() == {'messages': []}
        assert restarted.get('/api/conversation', headers=renewed).json() == before
        verify_answer(restarted, customer, source_id)
        assert restarted.get('/api/conversation', headers=renewed).json() == before
        verify_answer(restarted, renewed, source_id)
        messages = restarted.get('/api/conversation', headers=renewed).json()['messages']
        assert messages[:2] == before['messages']
        assert [message['role'] for message in messages] == ['user', 'assistant', 'user', 'assistant']


def test_http_journal_recovery_after_restart_and_model_failure(server):
    process, client = server('interrupt')
    with client:
        admin, customer = login(client, 'admin'), login(client, 'customer')
        before_count = client.get('/api/knowledge/items', headers=admin).json()['total']
        assert client.post('/api/knowledge/ingest', headers=admin, json=DOCUMENT).status_code == 500
        pending = client.get('/api/knowledge/ingest/pending', headers=admin).json()['items']
        assert len(pending) == 1 and pending[0]['stage'] == 'chunked'
        fingerprint = pending[0]['fingerprint']
        assert client.get('/api/knowledge/items', headers=admin).json()['total'] == before_count
        stop_server(process, str(client.base_url))
    _, restarted = server()
    with restarted:
        recovery_url = '/api/knowledge/ingest/recover/' + fingerprint
        assert restarted.post(recovery_url, headers=customer).status_code == 403
        restored = restarted.post(recovery_url, headers=admin)
        assert restored.status_code == 200 and restored.json()['created']
        assert not restarted.post(recovery_url, headers=admin).json()['created']
        assert restarted.get('/api/knowledge/ingest/pending', headers=admin).json() == {'items': []}
        verify_answer(restarted, customer, restored.json()['knowledge_id'])
        failed = restarted.post('/api/chat', headers=customer, json={'message':'__MODEL_TIMEOUT__'})
        assert failed.status_code == 200
        assert 'private-provider-detail' not in failed.text
        assert any(event.get('retryable') for event in failed.json()['trace'] if event['kind']=='error')
        assert restarted.get('/api/conversation', headers=customer).json()['messages'][-1]['content'] == failed.json()['reply']


def test_http_stage_filter_pagination_and_recovery_across_restarts(server):
    pending_url = '/api/knowledge/ingest/pending'
    documents = {}
    # 通过实际入库链路生成两种中断状态，不直接修改 journal。
    for mode, stage, count in [('interrupt_parsed', 'parsed', 3), ('interrupt', 'chunked', 21)]:
        process, client = server(mode)
        with client:
            admin = login(client, 'admin')
            before = client.get('/api/knowledge/items', headers=admin).json()['total']
            for index in range(count):
                document = {**DOCUMENT, 'title': f'E2E_{stage}_{index:02}',
                            'content': f'{stage}-{index}: preserved input for recovery.'}
                documents[document['title']] = document
                assert client.post('/api/knowledge/ingest', headers=admin, json=document).status_code == 500
            page = client.get(pending_url, headers=admin, params={'stage': stage}).json()
            assert page['total'] == count
            assert len(page['items']) == count
            assert all(item['stage'] == stage for item in page['items'])
            assert all(item['chunk_count'] == (0 if stage == 'parsed' else 1) for item in page['items'])
            assert client.get('/api/knowledge/items', headers=admin).json()['total'] == before
            stop_server(process, str(client.base_url))

    process, client = server()
    with client:
        admin, customer = login(client, 'admin'), login(client, 'customer')

        def read(stage=None, offset=0, limit=20):
            params = {'offset': offset, 'limit': limit}
            if stage is not None:
                params['stage'] = stage
            response = client.get(pending_url, headers=admin, params=params)
            assert response.status_code == 200
            return response.json()

        assert client.get(pending_url, params={'stage': 'parsed'}).status_code == 403
        assert client.get(pending_url, headers=customer, params={'stage': 'chunked'}).status_code == 403
        for stage in ('', 'stored', 'unknown'):
            assert client.get(pending_url, headers=admin, params={'stage': stage}).status_code == 422
        assert read()['total'] == 24
        parsed = read('parsed')
        assert parsed['total'] == len(parsed['items']) == 3
        first, last = read('chunked'), read('chunked', 20)
        assert first['total'] == last['total'] == 21
        assert len(first['items']) == 20 and len(last['items']) == 1
        fingerprints = [row['fingerprint'] for row in first['items'] + last['items']]
        assert len(set(fingerprints)) == 21
        assert all(row['stage'] == 'chunked' and 'payload' not in row for row in first['items'] + last['items'])
        assert read('chunked', 21) == {'items': [], 'total': 21}
        before = client.get('/api/knowledge/items', headers=admin).json()['total']
        restored_ids = []
        # 恢复末页唯一一条后该页变空；另一阶段不受影响。
        for row, stage, remaining in [(last['items'][0], 'chunked', 20), (parsed['items'][0], 'parsed', 2)]:
            url = '/api/knowledge/ingest/recover/' + row['fingerprint']
            assert client.post(url, headers=customer).status_code == 403
            response = client.post(url, headers=admin)
            assert response.status_code == 200
            result = response.json()
            assert result['created'] and result['knowledge_id'] == row['knowledge_id']
            restored_ids.append(result['knowledge_id'])
            source = client.get('/api/sources/' + result['knowledge_id'])
            assert source.status_code == 200
            document = documents[source.json()['title']]
            assert source.json()['content'] == document['content']
            assert read(stage)['total'] == remaining
            repeated = client.post(url, headers=admin)
            assert repeated.status_code == 200 and not repeated.json()['created']
            assert repeated.json()['knowledge_id'] == result['knowledge_id']
        assert read('chunked', 20) == {'items': [], 'total': 20}
        assert len(read('chunked')['items']) == 20
        assert read()['total'] == 22
        assert client.get('/api/knowledge/items', headers=admin).json()['total'] == before + 2
        stop_server(process, str(client.base_url))
    _, restarted = server()
    with restarted:
        for stage, total in [('parsed', 2), ('chunked', 20)]:
            response = restarted.get(pending_url, headers=admin, params={'stage': stage})
            assert response.status_code == 200 and response.json()['total'] == total
            assert not set(restored_ids) & {row['knowledge_id'] for row in response.json()['items']}
        for source_id in restored_ids:
            assert restarted.get('/api/sources/' + source_id).status_code == 200
