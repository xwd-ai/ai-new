"""最近会话选择规则通过临时 SQLite 验证。"""

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Conversation, Message
from backend.services import chat


@pytest.fixture()
def selection_store(tmp_path, monkeypatch):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'selection.db'}", connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(chat, 'SessionLocal', factory)
    yield factory
    engine.dispose()


def populate(factory, rows):
    with factory() as db:
        for key, created, updated in rows:
            db.add(Conversation(id=key, user_id='user', title='', created_at=created, updated_at=updated))
            db.add(Message(id='msg-' + key, conversation_id=key, role='user', content=key, at=updated))
        # 其他用户的时间和编号均更大，也不得参与选择。
        db.add(Conversation(id='zz-private', user_id='other', title='', created_at=999, updated_at=999))
        db.add(Message(id='private', conversation_id='zz-private', role='user', content='private', at=999))
        db.commit()


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('rows, expected', [
    ([('a', 1, 100), ('z', 99, 90)], 'a'),
    ([('z', 1, 100), ('a', 2, 100)], 'a'),
    ([('a', 2, 100), ('z', 2, 100)], 'z'),
])
def test_latest_selection_uses_updated_created_then_id(selection_store, reverse, rows, expected):
    factory = selection_store
    populate(factory, list(reversed(rows)) if reverse else rows)
    for _ in range(2):
        assert chat.get_or_create_conversation('user') == expected
    with factory() as db:
        assert db.query(Conversation).count() == 3
        assert sorted((c.id, c.created_at, c.updated_at) for c in db.query(Conversation)
                      .filter(Conversation.user_id == 'user')) == sorted(rows)
