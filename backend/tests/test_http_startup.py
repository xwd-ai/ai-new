import json
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
    message = http_startup.failure_detail(SimpleNamespace(poll=lambda: code), tmp_path, 0)
    assert f'reason={reason}' in message and f'exit_code={code}' in message
    assert 'elapsed_s=' in message and str(tmp_path / 'server.log') in message
    assert 'migration pending\nthread stack' in message


def test_real_worker_reports_startup_phases(server, tmp_path):
    _, client = server()
    with client:
        assert client.get('/health').status_code == 200
    lines = (tmp_path / '0' / 'server.log').read_text('utf-8').splitlines()
    records = [json.loads(line) for line in lines if line.startswith('{"startup_stage"')]
    assert [r['startup_stage'] for r in records if r['state'] == 'completed'] == [
        'imports', 'migration', 'services', 'app_init', 'listener',
    ]
    assert all(r['elapsed_s'] >= 0 for r in records if r['state'] == 'completed')
