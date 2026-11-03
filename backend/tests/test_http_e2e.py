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
