"""恢复页面与脚本的冒烟测试。"""

from backend.tests.test_api_contracts import api_client


def test_recovery_page_and_script_are_served(api_client):
    page = api_client.get("/")
    assert page.status_code == 200
    assert 'id="syncHistory"' in page.text
    assert '/assets/chat-recovery.js' in page.text
    assert api_client.get('/assets/chat-recovery.js').status_code == 200
