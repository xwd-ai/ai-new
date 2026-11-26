"""同时间戳消息在页面回放与模型窗口中使用同一稳定顺序。"""

import pytest

from backend.models import Conversation, Message
from backend.services import chat
from backend.tests.test_conversation_selection import selection_store


def seed_messages(factory, reverse=False, tie_count=11):
    rows = [('z-old', 1, 'user')] + [
        (f'm{i:02}', 2, 'user' if i % 2 == 0 else 'assistant') for i in range(tie_count)
    ] + [('a-new', 3, 'assistant')]
    # 故意打乱同时间戳的插入顺序，避免依赖 SQLite 的行存储顺序。
    inserted = rows[::2] + rows[1::2]
    with factory() as db:
        db.add_all([
            Conversation(id='mine', user_id='user', title='', created_at=1, updated_at=2),
            Conversation(id='old-session', user_id='user', title='', created_at=0, updated_at=0),
            Conversation(id='private', user_id='other', title='', created_at=9, updated_at=9),
        ])
        for key, at, role in reversed(inserted) if reverse else inserted:
            db.add(Message(id=key, conversation_id='mine', role=role, content=key, at=at))
        db.add_all([
            Message(id='old-message', conversation_id='old-session', role='user', content='old session', at=99),
            Message(id='private-message', conversation_id='private', role='user', content='private', at=99),
        ])
        db.commit()
    return sorted(rows, key=lambda row: (row[1], row[0]))


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('limit', [1, 3, 8, 30])
def test_history_window_uses_stable_order(selection_store, reverse, limit):
    factory = selection_store
    rows = seed_messages(factory, reverse)
    expected = [{'role': role, 'content': key, 'at': float(at)} for key, at, role in rows]
    for _ in range(2):
        assert chat.history('mine', limit=limit) == [
            {'role': row['role'], 'content': row['content']} for row in expected[-limit:]
        ]
    with factory() as db:
        assert db.query(Message).count() == len(rows) + 2
        assert sorted((m.id, m.at, m.role) for m in db.query(Message)
                      .filter(Message.conversation_id == 'mine')) == sorted(rows)
        assert db.get(Conversation, 'mine').updated_at == 2


@pytest.mark.parametrize('tie_count', [10, 11])
def test_model_receives_the_stable_recent_window(selection_store, monkeypatch, tie_count):
    factory = selection_store
    rows = seed_messages(factory, tie_count=tie_count)
    monkeypatch.setattr(chat.orders, 'query_order', lambda _: None)
    monkeypatch.setattr(chat.knowledge, 'search_knowledge', lambda _: [])
    seen = []
    def step(message, **kwargs):
        seen.extend(kwargs['history'])
        return 'answer'
    monkeypatch.setattr(chat.llm, 'chat_step', step)
    reply, _, _ = chat.build_reply('user', 'question')
    assert reply == 'answer'
    expected = [{'role': role, 'content': key} for key, _, role in rows[-8:]]
    while expected and expected[0]['role'] != 'user':
        expected.pop(0)
    assert seen == expected
