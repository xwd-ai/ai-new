"""首次会话竞争测试的独立进程，仅连接命令行指定的临时库。"""

import json
from pathlib import Path
import sys
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.services import chat


def main():
    database, directory, worker = sys.argv[1:]
    directory = Path(directory)
    engine = create_engine(f'sqlite:///{database}')
    chat.SessionLocal = sessionmaker(bind=engine, autoflush=False)
    first = True

    @event.listens_for(engine, 'after_cursor_execute')
    def pause_first_read(conn, cursor, statement, parameters, context, executemany):
        nonlocal first
        if first and 'FROM conversations' in statement:
            first = False
            (directory / f'{worker}.ready').write_text('ready')
            deadline = time.monotonic() + 15
            while not (directory / 'go').exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('parent did not release initial reads')
                time.sleep(0.02)

    try:
        print(json.dumps(chat.get_or_create_conversation('shared-user')))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
