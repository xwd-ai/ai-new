"""将旧端口发布的空文件窗口固定下来，验证真实父子进程不会缓存错误地址。"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import httpx

from backend.tests import test_http_e2e as e2e


def test_empty_port_window_does_not_pin_probe_to_default_port(tmp_path, monkeypatch):
    parent_read_empty = threading.Event()
    original_read_port = e2e.read_port
    def observe_port_read(run):
        port = original_read_port(run)
        if port is None and (run / 'empty.ready').exists():
            parent_read_empty.set()
        return port
    monkeypatch.setattr(e2e, 'read_port', observe_port_read)
    wrapper = tmp_path / 'slow_port_worker.py'
    wrapper.write_text('''
from pathlib import Path
import time
from backend.tests import http_e2e_worker as worker

def slow_publish(run, port):
    # 精确复现旧 write_text 创建空文件、父端先读、再写正文的顺序。
    with (run / 'port.txt').open('w', encoding='ascii') as stream:
        (run / 'empty.ready').write_text('ready')
        deadline = time.monotonic() + 25
        while not (run / 'release').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('parent did not release port writer')
            time.sleep(0.01)
        stream.write(str(port))

worker.publish_port = slow_publish
worker.main()
''', encoding='utf-8')
    monkeypatch.setattr(e2e, 'WORKER', wrapper)
    run = tmp_path / '0'
    with e2e.server_pool(tmp_path) as (start, entries), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(start)
        try:
            deadline = time.monotonic() + 25
            while not (run / 'empty.ready').exists():
                if future.done():
                    future.result()
                    raise AssertionError('worker unexpectedly became ready before publishing port')
                assert time.monotonic() < deadline, (run / 'server.log').read_text('utf-8')
                time.sleep(0.01)
            # 旧客户端会永久缓存此 URL。仅解析地址，避免向错误端口发送请求。
            raw = (run / 'port.txt').read_text('ascii').strip()
            assert raw == ''
            assert parent_read_empty.wait(timeout=5), 'parent did not poll during empty-file window'
            with httpx.Client(base_url='http://127.0.0.1:' + raw, trust_env=False) as old_client:
                assert old_client.base_url.port is None
                assert old_client.base_url.scheme == 'http'  # HTTP 默认端口为 80。
            assert e2e.read_port(run) is None
            assert entries[0]['url'] is None
        finally:
            if run.exists():
                (run / 'release').write_text('go', encoding='ascii')
        process, client = future.result(timeout=25)
        with client:
            assert client.base_url.port == int((run / 'port.txt').read_text('ascii'))
            assert client.get('/health').status_code == 200
        assert process.poll() is None
    assert entries[0]['process'].poll() is not None
    assert entries[0]['output'].closed
