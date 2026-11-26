"""知识库管理权限复用统一认证，保持既有 403 detail 契约。"""

import time

import pytest

from backend.models import User
from backend.models.user import RevokedToken
from backend.services import auth, chat, knowledge
from backend.tests.test_api_contracts import api_client, _headers


ENDPOINTS = [
    ('POST', '/api/knowledge/ingest', {'title': 'permissions', 'content': 'test input', 'kind': 'faq'}, 200),
    ('GET', '/api/knowledge/diagnose?q=permissions', None, 200),
    ('POST', '/api/knowledge/ingest/recover/' + 'a' * 64, None, 404),
]


@pytest.mark.parametrize('method,path,kwargs', [
    ('POST', '/api/knowledge/ingest', {'json': {}}),
    ('GET', '/api/knowledge/diagnose', {}),
    ('GET', '/api/knowledge/diagnose', {'params': {'q': 'permissions', 'top_k': 'bad'}}),
])
def test_permission_dependency_precedes_field_validation(api_client, method, path, kwargs):
    assert api_client.request(method, path, **kwargs).status_code == 403
    assert api_client.request(method, path, headers=_headers(api_client), **kwargs).status_code == 422


def test_diagnosis_denial_does_not_run_retrieval(api_client, monkeypatch):
    calls = []
    original = knowledge.diagnose

    def observe(q, top_k):
        calls.append((q, top_k))
        return original(q, top_k=top_k)

    monkeypatch.setattr(knowledge, 'diagnose', observe)
    for headers in ({}, _headers(api_client, 'customer')):
        for params in ({}, {'q': '导出'}, {'q': '导出', 'top_k': 'bad'}):
            response = api_client.get('/api/knowledge/diagnose', headers=headers, params=params)
            assert response.status_code == 403
            assert response.json() == {'detail': '需要管理员权限'}
    assert calls == []
    response = api_client.get('/api/knowledge/diagnose', headers=_headers(api_client), params={'q': '导出'})
    assert response.status_code == 200
    assert calls == [('导出', 3)]


@pytest.mark.parametrize('method,path,payload,success', ENDPOINTS)
def test_management_permissions_and_revocation(api_client, method, path, payload, success):
    def request(headers):
        return api_client.request(method, path, headers=headers, json=payload)

    for headers in ({}, {'Authorization': 'Bearer invalid'}, _headers(api_client, 'customer')):
        response = request(headers)
        assert response.status_code == 403
        assert response.json() == {'detail': '需要管理员权限'}
    admin = _headers(api_client)
    assert request(admin).status_code == success
    auth.logout(auth.extract_bearer_token(admin['Authorization']))
    assert request(admin).status_code == 403


@pytest.mark.parametrize('method,path,payload,success', ENDPOINTS)
@pytest.mark.parametrize('state', ['expired', 'revoked', 'demoted'])
def test_invalid_admin_cannot_be_bypassed_by_live_session(api_client, method, path, payload, success, state):
    # 会话登记仍有效时，也必须检查 JWT 本身、吊销记录及当前角色。
    with auth.SessionLocal() as db:
        user = db.get(User, 'u-admin').to_dict()
    claims = auth.build_access_payload(user, ttl=3600,
        issued_at=int(time.time()) - (86400 if state == 'expired' else 0))
    token = auth.sign_jwt(claims)
    auth._record_session(user, token, time.time() + 3600)
    with auth.SessionLocal() as db:
        if state == 'revoked':
            db.add(RevokedToken(jti=claims['jti'], user_id=user['id'], token_type='access',
                expires_at=claims['exp'], revoked_at=time.time(), reason='test'))
        elif state == 'demoted':
            db.get(User, user['id']).role = 'customer'
        db.commit()
    identity = chat.current_user('Bearer ' + token)
    if state == 'demoted':
        assert identity['role'] == 'customer'
    else:
        assert identity is None
    response = api_client.request(method, path, json=payload, headers={'Authorization': 'Bearer ' + token})
    assert response.status_code == 403
    assert response.json() == {'detail': '需要管理员权限'}
