"""恢复页面及历史用户边界的隔离冒烟。"""

from backend.models import Conversation, Message
from backend.services import auth
from backend.tests.test_api_contracts import api_client, _headers


def test_recovery_page_and_script_are_served(api_client):
    page = api_client.get("/")
    assert page.status_code == 200
    assert 'id="syncHistory"' in page.text
    assert '/assets/chat-recovery.js' in page.text
    assert api_client.get('/assets/chat-recovery.js').status_code == 200


def test_history_recovery_is_ordered_and_does_not_cross_users(api_client):
    headers = _headers(api_client, 'customer')
    with auth.SessionLocal() as db:
        for user_id, conv_id in [('u-customer', 'mine'), ('other', 'theirs')]:
            db.add(Conversation(id=conv_id, user_id=user_id, title='', created_at=1, updated_at=2))
        db.add_all([
            Message(id='answer', conversation_id='mine', role='assistant', content='已保存回答', at=2),
            Message(id='question', conversation_id='mine', role='user', content='原问题', at=1),
            Message(id='private', conversation_id='theirs', role='user', content='其他用户消息', at=3),
        ])
        db.commit()
    for _ in range(2):
        result = api_client.get('/api/conversation', headers=headers)
        assert result.status_code == 200
        assert [item['content'] for item in result.json()['messages']] == ['原问题', '已保存回答']
    assert api_client.get('/api/conversation', headers={'Authorization': 'Bearer invalid'}).json() == {'messages': []}
