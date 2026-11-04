"""API 契约与示例链路使用独立数据库验证，不调用真实模型。"""

import hashlib
import importlib

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend import database, models, seed
from backend.api import chat as chat_api
from backend.services import auth, chat, ingest, knowledge, orders


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
    )
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (database, chat_api, auth, chat, ingest, knowledge, orders):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(database, "init_db", lambda: None)
    monkeypatch.setattr(seed, "seed_all", lambda: None)
    monkeypatch.setattr(ingest, "_checkpoint_path", lambda: tmp_path / "checkpoints.json")

    class CitingProvider:
        def chat(self, *args, **kwargs):
            return '使用 CSV 导出。<ref id="c1"/>'

    monkeypatch.setattr(chat, "_chat_provider", CitingProvider())
    with factory() as db:
        for role in ("admin", "customer"):
            db.add(models.User(
                id=f"u-{role}", username=role, name=role, role=role,
                password_hash=hashlib.sha256(f"{role}123".encode()).hexdigest(),
            ))
        db.commit()
    main = importlib.import_module("backend.main")
    with chat.execution_scope(), TestClient(main.app) as client:
        yield client
    engine.dispose()


def _headers(client, role="admin"):
    response = client.post("/api/login", json={"username": role, "password": f"{role}123"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_knowledge_page_assets_and_management_navigation(api_client):
    page = api_client.get('/knowledge')
    assert page.status_code == 200
    assert '<title>知识资料' in page.text
    assert '/assets/knowledge-browser.js' in page.text
    script = api_client.get('/assets/knowledge-browser.js')
    assert script.status_code == 200 and 'class KnowledgeBrowser' in script.text
    for path in ('/admin', '/settings'):
        assert 'href="/knowledge"' in api_client.get(path).text


def test_openapi_describes_typed_responses_and_error_statuses(api_client):
    spec = api_client.get("/openapi.json").json()
    for path, method, model in (
        ("/api/chat", "post", "ChatResponse"),
        ("/api/conversation", "get", "ConversationResponse"),
        ("/api/knowledge/items", "get", "KnowledgeListResponse"),
        ("/api/knowledge/diagnose", "get", "KnowledgeDiagnosisResponse"),
        ("/health", "get", "HealthResponse"),
    ):
        schema = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model}"}
    for path in ("/api/knowledge/ingest", "/api/knowledge/items", "/api/knowledge/diagnose"):
        operation = spec["paths"][path]["post" if path.endswith("ingest") else "get"]
        assert "403" in operation["responses"]
    assert "404" in spec["paths"]["/api/sources/{source_id}"]["get"]["responses"]
    chat_schema = spec["components"]["schemas"]["ChatResponse"]
    assert chat_schema["properties"]["sources"]["items"] == {"$ref": "#/components/schemas/SourceSummary"}
    assert spec["components"]["schemas"]["ChatRequest"]["properties"]["message"]["examples"]


def test_documented_login_ingest_source_diagnose_chat_and_history_flow(api_client):
    headers = _headers(api_client)
    payload = {"title": "导出 FAQ", "content": "导出失败时请检查权限，并使用 CSV 格式。", "kind": "faq", "keywords": ["导出"]}
    created = api_client.post("/api/knowledge/ingest", json=payload, headers=headers)
    assert created.status_code == 200
    created = created.json()
    assert created["created"] is True and created["chunks"] == 1 and created["stage"] == "stored"
    source_id = created["knowledge_id"]
    repeated = api_client.post("/api/knowledge/ingest", json=payload, headers=headers).json()
    assert repeated["knowledge_id"] == source_id
    assert repeated["created"] is False and repeated["chunks"] == 0
    listed = api_client.get("/api/knowledge/items", headers=headers).json()
    assert listed == {"total": 1, "items": [{"id": source_id, "title": payload["title"], "kind": "faq"}]}
    source = api_client.get(f"/api/sources/{source_id}")
    assert source.status_code == 200
    assert source.json()["content"] == payload["content"]
    diagnosis = api_client.get("/api/knowledge/diagnose", params={"q": "导出", "top_k": 3}, headers=headers)
    assert diagnosis.status_code == 200 and source_id in diagnosis.json()["final_ids"]
    customer = _headers(api_client, "customer")
    reply = api_client.post("/api/chat", json={"message": "导出失败怎么办"}, headers=customer)
    assert reply.status_code == 200
    body = reply.json()
    assert set(body) == {"reply", "sources", "suggest_ticket", "trace"}
    assert body["reply"] == "使用 CSV 导出。[来源: 导出 FAQ]"
    assert body["sources"] == [{"id": source_id, "handle": "c1", "title": payload["title"], "kind": "faq"}]
    assert body["trace"][-1]["kind"] == "done"
    history = api_client.get("/api/conversation", headers=customer).json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == body["reply"]
    assert isinstance(history[-1]["at"], float)


@pytest.mark.parametrize("identity", [None, "customer"])
def test_admin_operations_reject_anonymous_and_customer(api_client, identity):
    headers = _headers(api_client, identity) if identity else {}
    for method, path, kwargs in (
        ("post", "/api/knowledge/ingest", {"json": {"title": "FAQ", "content": "正文"}}),
        ("get", "/api/knowledge/items", {}),
        ("get", "/api/knowledge/diagnose", {"params": {"q": "导出"}}),
    ):
        response = getattr(api_client, method)(path, headers=headers, **kwargs)
        assert response.status_code == 403
        assert response.json() == {"detail": "需要管理员权限"}


@pytest.mark.parametrize("payload", [
    {"title": "", "content": "正文"},
    {"title": "FAQ", "content": " "},
    {"title": "FAQ", "content": "正文", "kind": "file"},
    {"title": "FAQ", "content": "正文", "keywords": [str(i) for i in range(21)]},
])
def test_ingest_validation_returns_422(api_client, payload):
    response = api_client.post("/api/knowledge/ingest", json=payload, headers=_headers(api_client))
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_ingest_service_validation_uses_string_detail(api_client, monkeypatch):
    def fail(**kwargs):
        raise ValueError("正文不能为空")

    monkeypatch.setattr(ingest, "ingest", fail)
    response = api_client.post("/api/knowledge/ingest", json={"title": "FAQ", "content": "正文"}, headers=_headers(api_client))
    assert response.status_code == 422
    assert response.json() == {"detail": "正文不能为空"}


@pytest.mark.parametrize("requested,effective", [(0, 1), (99, 20)])
def test_diagnosis_top_k_is_clamped_instead_of_rejected(api_client, requested, effective):
    response = api_client.get("/api/knowledge/diagnose", params={"q": "导出", "top_k": requested}, headers=_headers(api_client))
    assert response.status_code == 200
    assert response.json()["top_k"] == effective


def test_missing_source_and_anonymous_history_contracts(api_client):
    response = api_client.get("/api/sources/not-found")
    assert response.status_code == 404
    assert response.json() == {"detail": "来源不存在"}
    assert api_client.get("/api/conversation").json() == {"messages": []}


def test_health_only_reports_configuration(api_client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    response = api_client.get("/health")
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "ok"
    assert report["models"]["all_available"] is False
    assert {c["capability"] for c in report["models"]["capabilities"]} == {"chat", "embedding", "rerank"}
    assert all(not c["available"] for c in report["models"]["capabilities"])


def test_chat_input_keeps_existing_empty_string_and_validation_behavior(api_client):
    assert api_client.post("/api/chat", json={"message": ""}).status_code == 200
    for payload in ({}, {"message": []}):
        response = api_client.post("/api/chat", json=payload)
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], list)


def test_invalid_login_and_token_keep_existing_fallbacks(api_client):
    response = api_client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert response.status_code == 200
    assert response.json() == {"token": "", "user": {}}
    headers = {"Authorization": "Bearer invalid-token"}
    assert api_client.get("/api/conversation", headers=headers).json() == {"messages": []}
    assert api_client.post("/api/chat", headers=headers, json={"message": "导出失败"}).status_code == 200
