"""真实失败子进程必须在测试池退出时回收，不以端口文件视为健康。"""

import httpx
import pytest

from backend.tests import test_http_e2e as e2e


@pytest.mark.parametrize('mode, reason, probe', [
    ('exit_before_port', 'process_exited', 'port_not_published'),
    ('unhealthy', 'startup_timeout', 'HTTP 503'),
    ('blocked_listener', 'startup_timeout', 'ReadTimeout'),
])
def test_failed_startup_reports_and_reaps_processes(tmp_path, mode, reason, probe):
    with e2e.server_pool(tmp_path) as (start, processes):
        with pytest.raises(pytest.fail.Exception) as caught:
            start(mode, timeout=10)
        message = str(caught.value)
        assert f'reason={reason}' in message and f'last_probe={probe}' in message
        assert str(tmp_path / '0' / 'server.log') in message
        assert len(processes) == 1
        entry = processes[0]
        if mode == 'exit_before_port':
            assert 'injected failure before port publication' in message
            assert entry['url'] is None and entry['process'].poll() != 0
        else:
            assert entry['url'] in message
            assert '"startup_stage": "listener", "state": "completed"' in message
            assert '"startup_stage": "health", "state": "completed"' not in message
            assert entry['process'].poll() is None
    assert entry['process'].poll() is not None
    if mode == 'blocked_listener':
        assert entry['process'].returncode != 0  # 没有运行 HTTP 退出路由，只能走强制回收。
    assert entry['output'].closed and entry['process']._e2e_stopped
    # 已回收后的再次清理是幂等操作。
    e2e.stop_server(entry['process'], entry['url'])
    if entry['url']:
        with httpx.Client(base_url=entry['url'], timeout=1, trust_env=False) as client:
            with pytest.raises(httpx.TransportError):
                client.get('/health')


def test_callers_exception_still_closes_healthy_process_and_log(tmp_path):
    with pytest.raises(RuntimeError, match='injected caller failure'):
        with e2e.server_pool(tmp_path) as (start, processes):
            _, client = start()
            with client:
                assert client.get('/health').status_code == 200
            entry = processes[0]
            raise RuntimeError('injected caller failure')
    assert entry['process'].poll() is not None
    assert entry['process']._e2e_stopped and entry['output'].closed
