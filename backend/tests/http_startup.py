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


def failure_detail(process, run, started):
    code = process.poll()
    reason = 'startup_timeout' if code is None else 'process_exited'
    return (f'HTTP 服务启动失败：reason={reason}, exit_code={code}, '
            f'elapsed_s={time.monotonic() - started:.3f}, log={run / "server.log"}\n'
            + (run / 'server.log').read_text('utf-8'))
