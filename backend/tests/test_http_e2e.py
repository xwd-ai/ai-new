"""真实本机 HTTP、独立进程与临时 SQLite；不调用外部模型。"""

import os
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest

from backend.services import auth, ingest
from backend.tests.http_startup import failure_detail, read_port


ROOT = Path(__file__).resolve().parents[2]
JWT_SECRET = b"isolated-http-test-signing-secret-only"
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
            env={**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONIOENCODING': 'utf-8',
                 'AI_NEW_JWT_SECRET': JWT_SECRET.decode()},
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


def headers_for(role):
    payload = auth.build_access_payload({"id": f"u-{role}", "username": role, "role": role})
    return {"Authorization": "Bearer " + auth.sign_jwt(payload, secret=JWT_SECRET)}


DOCUMENT = {'title': 'E2E_GUIDE', 'content': 'e2eexport: choose CSV and check export permission.',
            'kind': 'faq', 'keywords': ['e2eexport']}


def test_http_ingest_and_diagnosis_survive_restart(server):
    process, client = server()
    admin = headers_for('admin')
    with client:
        assert client.post('/api/knowledge/ingest', headers=headers_for('customer'),
                           json=DOCUMENT).status_code == 403
        created = client.post('/api/knowledge/ingest', headers=admin, json=DOCUMENT).raise_for_status().json()
        assert created['created']
        stop_server(process, str(client.base_url))

    _, restarted = server()
    with restarted:
        repeated = restarted.post('/api/knowledge/ingest', headers=admin,
                                  json=DOCUMENT).raise_for_status().json()
        assert not repeated['created']
        assert repeated['knowledge_id'] == created['knowledge_id']
        diagnosis = restarted.get('/api/knowledge/diagnose', headers=admin,
                                  params={'q': 'e2eexport'}).raise_for_status().json()
        assert created['knowledge_id'] in diagnosis['final_ids']


@pytest.mark.parametrize('mode', ['interrupt_parsed', 'interrupt'])
def test_http_ingest_recovery_across_restarts(server, mode):
    process, client = server(mode)
    admin = headers_for('admin')
    with client:
        assert client.post('/api/knowledge/ingest', headers=admin, json=DOCUMENT).status_code == 500
        stop_server(process, str(client.base_url))

    _, restarted = server()
    with restarted:
        fingerprint = ingest.content_fingerprint(DOCUMENT['title'], DOCUMENT['content'])
        url = '/api/knowledge/ingest/recover/' + fingerprint
        assert restarted.post(url, headers=headers_for('customer')).status_code == 403
        restored = restarted.post(url, headers=admin).raise_for_status().json()
        assert restored['created']
        repeated = restarted.post(url, headers=admin).raise_for_status().json()
        assert not repeated['created']
        assert repeated['knowledge_id'] == restored['knowledge_id']
        diagnosis = restarted.get('/api/knowledge/diagnose', headers=admin,
                                  params={'q': 'e2eexport'}).raise_for_status().json()
        assert restored['knowledge_id'] in diagnosis['final_ids']
