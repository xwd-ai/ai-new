"""仅供 HTTP 集成测试启动的隔离服务，不是部署入口。"""

from pathlib import Path
import faulthandler
import json
import re
import socket
import sys
import time

from backend.tests.http_startup import publish_port, stage


def main():
    root, run, mode = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3]
    repository = Path(__file__).resolve().parents[2]
    if root == repository or repository in root.parents or root not in run.parents:
        raise ValueError('测试数据库和服务日志必须放在仓库外的临时目录')
    if mode == 'exit_before_port':
        raise RuntimeError('injected failure before port publication')

    # 原超时日志只有 Alembic 的通用提示；慢启动时同时保留具体线程栈。
    faulthandler.dump_traceback_later(10, repeat=True)
    with stage('imports'):
        import sqlalchemy as sa
        from sqlalchemy.orm import sessionmaker
        from backend import database, models
        from alembic import command
        from alembic.config import Config

    engine = sa.create_engine(f"sqlite:///{root / 'http.db'}", connect_args={'check_same_thread':False})
    # 在导入业务模块及 main 之前切换全部数据库入口；真实启动与 seed 仍运行。
    database.engine = database._engine = engine
    database.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with stage('migration'), engine.connect() as connection:
        config = Config(str(repository / 'alembic.ini'))
        config.attributes['connection'] = connection
        command.upgrade(config, 'head')
        connection.commit()

    with stage('services'):
        from backend.services import chat, ingest, llm, retrieval_config
    ingest._checkpoint_path = lambda: root / 'checkpoints.json'
    retrieval_config._settings_path = lambda: root / 'retrieval_settings.json'

    class LocalProvider:
        def chat(self, messages):
            if messages[-1]['content'] == '__MODEL_TIMEOUT__':
                raise TimeoutError('private-provider-detail')
            evidence = '\n'.join(m['content'] for m in messages if m['role']=='system'
                                 and m['content'].startswith('以下是相关知识库资料'))
            match = re.search(r'\[(c\d+)\] E2E_GUIDE\n([^\n]+)', evidence)
            if match is None:
                raise AssertionError('没有收到真实召回的测试资料')
            return f'{match[2]} <ref id="{match[1]}"/> <ref id="c999"/>'

    provider = LocalProvider()
    chat._chat_provider = llm._default_provider = provider

    def reject_external_provider():
        raise AssertionError('HTTP 测试禁止装配外部模型')
    llm.build_chat_provider = reject_external_provider

    if mode == 'interrupt':
        original = ingest._journal_chunked
        def interrupted(*args):
            original(*args)
            raise RuntimeError('测试注入：分块 journal 提交后中断')
        ingest._journal_chunked = interrupted

    with stage('app_init'):
        from backend.main import app
        import uvicorn
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        with stage('listener'):
            listener.bind(('127.0.0.1', 0))
            listener.listen(128)
            publish_port(run, listener.getsockname()[1])
            server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        health_started = time.monotonic()
        healthy = False
        print(json.dumps({'startup_stage': 'health', 'state': 'started'}), flush=True)
        @app.middleware('http')
        async def observe_health(request, call_next):
            nonlocal healthy
            if mode == 'unhealthy' and request.url.path == '/health':
                from starlette.responses import JSONResponse
                return JSONResponse({'status': 'injected unhealthy'}, status_code=503)
            response = await call_next(request)
            if request.url.path == '/health' and response.status_code == 200 and not healthy:
                healthy = True
                faulthandler.cancel_dump_traceback_later()
                print(json.dumps({'startup_stage': 'health', 'state': 'completed',
                                  'elapsed_s': round(time.monotonic() - health_started, 3)}), flush=True)
            return response
        # 仅隔离测试 worker 装配此路由，生产 app 不包含该入口。
        @app.post('/__e2e_shutdown')
        def shutdown():
            server.should_exit = True
            return {'stopping': True}
        try:
            if mode == 'blocked_listener':
                time.sleep(60)  # 注入监听完成但尚不处理请求的故障，只在测试 worker 中存在。
            server.run(sockets=[listener])
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    main()
