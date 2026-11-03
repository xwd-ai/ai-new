import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from backend.tests import http_startup
from backend.tests.test_http_e2e import server


def test_stage_records_completed_duration(monkeypatch, capsys):
    ticks = iter([10.0, 12.25])
    monkeypatch.setattr(http_startup.time, 'monotonic', lambda: next(ticks))
    with http_startup.stage('migrate'):
        pass
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {'startup_stage': 'migrate', 'state': 'started'},
        {'startup_stage': 'migrate', 'state': 'completed', 'elapsed_s': 2.25},
    ]


def test_stage_preserves_failure_and_reports_phase(capsys):
    error = RuntimeError('injected migration failure')
    with pytest.raises(RuntimeError) as caught:
        with http_startup.stage('migrate'):
            raise error
    assert caught.value is error
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1]['state'] == 'failed' and records[-1]['startup_stage'] == 'migrate'


@pytest.mark.parametrize('code, reason', [(None, 'startup_timeout'), (3, 'process_exited')])
def test_failure_report_distinguishes_exit_from_timeout(tmp_path, code, reason):
    (tmp_path / 'server.log').write_text('migration pending\nthread stack', encoding='utf-8')
    message = http_startup.failure_detail(SimpleNamespace(poll=lambda: code), tmp_path, 0,
                                          'http://127.0.0.1:43210', 'ConnectError')
    assert f'reason={reason}' in message and f'exit_code={code}' in message
    assert 'elapsed_s=' in message and str(tmp_path / 'server.log') in message
    assert 'migration pending\nthread stack' in message
    assert 'url=http://127.0.0.1:43210' in message and 'last_probe=ConnectError' in message


def test_real_worker_reports_startup_phases(server, tmp_path):
    _, client = server()
    with client:
        assert client.get('/health').status_code == 200
    lines = (tmp_path / '0' / 'server.log').read_text('utf-8').splitlines()
    records = [json.loads(line) for line in lines if line.startswith('{"startup_stage"')]
    assert [r['startup_stage'] for r in records if r['state'] == 'completed'] == [
        'imports', 'migration', 'services', 'app_init', 'listener', 'health',
    ]
    assert all(r['elapsed_s'] >= 0 for r in records if r['state'] == 'completed')


def test_port_is_not_visible_while_writer_is_still_filling_file(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow_write(path, content, **kwargs):
        # 精确停在文件创建但正文尚未写入的窗口。
        with path.open('w', encoding='ascii') as stream:
            entered.set()
            assert release.wait(timeout=5)
            return stream.write(content)
    monkeypatch.setattr(Path, 'write_text', slow_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(http_startup.publish_port, tmp_path, 43210)
        try:
            assert entered.wait(timeout=5)
            assert not (tmp_path / 'port.txt').exists()
        finally:
            release.set()
        future.result(timeout=5)
    assert (tmp_path / 'port.txt').read_text('ascii') == '43210'


@pytest.mark.parametrize('raw', ['', 'oops', '0', '65536', '-1', '12.3'])
def test_unusable_port_file_does_not_create_a_target(tmp_path, raw):
    (tmp_path / 'port.txt').write_text(raw, encoding='ascii')
    assert http_startup.read_port(tmp_path) is None


@pytest.mark.parametrize('port', [1, 43210, 65535])
def test_complete_port_is_read_after_publication(tmp_path, port):
    assert http_startup.read_port(tmp_path) is None
    http_startup.publish_port(tmp_path, port)
    assert http_startup.read_port(tmp_path) == port


def test_failed_publication_does_not_replace_existing_port(tmp_path, monkeypatch):
    http_startup.publish_port(tmp_path, 12345)
    def fail_write(path, content, **kwargs):
        with path.open('w', encoding='ascii') as stream:
            stream.write('4')
        raise OSError('injected incomplete write')
    monkeypatch.setattr(Path, 'write_text', fail_write)
    with pytest.raises(OSError, match='incomplete write'):
        http_startup.publish_port(tmp_path, 43210)
    assert http_startup.read_port(tmp_path) == 12345
