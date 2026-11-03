"""隔离 HTTP worker 的启动诊断，不用于生产应用。"""

from contextlib import contextmanager
import json
import time


@contextmanager
def stage(name):
    started = time.monotonic()
    print(json.dumps({'startup_stage': name, 'state': 'started'}), flush=True)
    try:
        yield
    except BaseException:
        print(json.dumps({'startup_stage': name, 'state': 'failed',
                          'elapsed_s': round(time.monotonic() - started, 3)}), flush=True)
        raise
    else:
        print(json.dumps({'startup_stage': name, 'state': 'completed',
                          'elapsed_s': round(time.monotonic() - started, 3)}), flush=True)


def publish_port(run, port):
    pending = run / 'port.pending'
    pending.write_text(str(port), encoding='ascii')
    pending.replace(run / 'port.txt')


def read_port(run):
    try:
        raw = (run / 'port.txt').read_text('ascii').strip()
    except (OSError, UnicodeError):
        return None
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 5):
        return None
    port = int(raw)
    return port if 1 <= port <= 65535 else None


def failure_detail(process, run, started, base_url=None, last_probe=None):
    code = process.poll()
    reason = 'startup_timeout' if code is None else 'process_exited'
    return (f'HTTP 服务启动失败：reason={reason}, exit_code={code}, '
            f'elapsed_s={time.monotonic() - started:.3f}, url={base_url}, '
            f'last_probe={last_probe}, log={run / "server.log"}\n'
            + (run / 'server.log').read_text('utf-8'))
