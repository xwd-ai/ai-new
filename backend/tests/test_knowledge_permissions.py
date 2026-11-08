"""知识库管理权限复用统一认证，保持既有 403 detail 契约。"""

import time

import pytest

from backend.models import User
from backend.models.user import RevokedToken
from backend.services import auth, chat, retrieval_config
from backend.tests.test_api_contracts import api_client, _headers


ENDPOINTS = [
    ('GET', '/api/knowledge/settings', None, 200),
    ('PUT', '/api/knowledge/settings', dict(prefetch_floor=12, prefetch_multiplier=2,
        rrf_k=40, vector_weight=0.6, keyword_weight=0.4, mmr_lambda=0.25), 200),
    ('GET', '/api/knowledge/items', None, 200),
    ('POST', '/api/knowledge/ingest', {'title': 'permissions', 'content': 'test input', 'kind': 'faq'}, 200),
    ('GET', '/api/knowledge/ingest/pending', None, 200),
    ('GET', '/api/knowledge/diagnose?q=permissions', None, 200),
    ('POST', '/api/knowledge/ingest/recover/' + 'a' * 64, None, 404),
]


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval_config, '_settings_path', lambda: tmp_path / 'settings.json')


@pytest.mark.parametrize('method,path,kwargs', [
    ('GET', '/api/knowledge/items', {'params': {'limit': 'bad'}}),
    ('POST', '/api/knowledge/ingest', {'json': {}}),
    ('PUT', '/api/knowledge/settings', {'json': {}}),
])
def test_permission_dependency_precedes_field_validation(api_client, method, path, kwargs):
    assert api_client.request(method, path, **kwargs).status_code == 403
    assert api_client.request(method, path, headers=_headers(api_client), **kwargs).status_code == 422


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
    assert api_client.post('/api/logout', headers=admin).status_code == 200
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
