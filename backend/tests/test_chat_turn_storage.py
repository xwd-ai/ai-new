"""同轮消息保存使用独立文件 SQLite，不接触应用数据库。"""

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models import Conversation, Message
from backend.services import chat


@pytest.fixture()
def turn_store(tmp_path, monkeypatch):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'turns.db'}")
    Base.metadata.create_all(engine)
    class TurnSession(Session):
        pass
    factory = sessionmaker(bind=engine, class_=TurnSession, autoflush=False)
    monkeypatch.setattr(chat, 'SessionLocal', factory)
    monkeypatch.setattr(chat.orders, 'query_order', lambda _: None)
    monkeypatch.setattr(chat.knowledge, 'search_knowledge', lambda _: [])
    monkeypatch.setattr(chat.llm, 'chat_step', lambda message, **kwargs: 'answer:' + message)
    with factory() as db:
        db.add(Conversation(id='conv', user_id='user', title='', created_at=1, updated_at=2))
        db.add_all([
            Message(id='old-user', conversation_id='conv', role='user', content='old', at=1),
            Message(id='old-assistant', conversation_id='conv', role='assistant', content='old answer', at=2),
        ])
        db.commit()
    with chat.execution_scope():
        yield engine, factory, TurnSession
    engine.dispose()


def stored(factory):
    # 重连查询，验证实际提交结果，不依赖写入 Session 的缓存。
    with factory() as db:
        messages = db.query(Message).order_by(Message.at).all()
        return [(m.role, m.content, m.at) for m in messages], db.get(Conversation, 'conv').updated_at


@pytest.mark.parametrize('failure', ['assistant_insert', 'commit'])
def test_failed_turn_rolls_back_both_messages_and_timestamp(turn_store, failure):
    engine, factory, session_class = turn_store
    before = stored(factory)
    if failure == 'assistant_insert':
        with engine.begin() as conn:
            conn.exec_driver_sql("""CREATE TRIGGER reject_assistant BEFORE INSERT ON messages
                WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'injected write failure'); END""")
        expected = sa.exc.IntegrityError
    else:
        def fail_commit(db):
            if any(isinstance(m, Message) and m.role == 'assistant' for m in db.new):
                db.flush()
                raise RuntimeError('injected commit failure')
        sa.event.listen(session_class, 'before_commit', fail_commit)
        expected = RuntimeError
    try:
        with pytest.raises(expected):
            chat.build_reply('user', 'new question')
        assert stored(factory) == before
        assert chat.last_summary() is None and chat.last_escalation() is None
    finally:
        if failure == 'assistant_insert':
            with engine.begin() as conn:
                conn.exec_driver_sql('DROP TRIGGER reject_assistant')
        else:
            sa.event.remove(session_class, 'before_commit', fail_commit)
    # 失败后重新调用只新增一轮，不遗留原问题或重复半轮。
    reply, _, _ = chat.build_reply('user', 'new question')
    messages, updated_at = stored(factory)
    assert [(role, content) for role, content, _ in messages] == [
        ('user', 'old'), ('assistant', 'old answer'),
        ('user', 'new question'), ('assistant', reply),
    ]
    assert updated_at == messages[-1][2]


@pytest.mark.parametrize('clock', [(100.0, 100.0), (100.0, 99.0), (100.0, 101.0)])
def test_successful_turn_is_complete_and_orders_user_before_assistant(turn_store, monkeypatch, clock):
    _, factory, _ = turn_store
    # 只替换 chat 模块的时钟，避免影响 Agent 计时和 pytest。
    from types import SimpleNamespace
    from itertools import chain, repeat
    ticks = chain([clock[0]], repeat(clock[1]))
    monkeypatch.setattr(chat, 'time', SimpleNamespace(time=lambda: next(ticks)))
    reply, sources, suggest = chat.build_reply('user', 'question')
    messages, updated_at = stored(factory)
    assert reply == 'answer:question' and sources == [] and isinstance(suggest, bool)
    assert [(role, content) for role, content, _ in messages][-2:] == [
        ('user', 'question'), ('assistant', reply),
    ]
    assert messages[-2][2] < messages[-1][2] == updated_at
    assert chat.history('conv')[-2:] == [
        {'role': 'user', 'content': 'question'}, {'role': 'assistant', 'content': reply},
    ]
