"""API 契约与示例链路使用独立数据库验证，不调用真实模型。"""

import importlib

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend import database, models, seed
from backend.services import auth, ingest, knowledge


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
    )
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (database, auth, ingest, knowledge):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(database, "init_db", lambda: None)
    monkeypatch.setattr(seed, "seed_all", lambda: None)
    monkeypatch.setattr(ingest, "_checkpoint_path", lambda: tmp_path / "checkpoints.json")

    with factory() as db:
        for role in ("admin", "customer"):
            db.add(models.User(
                id=f"u-{role}", username=role, name=role, role=role, password_hash="",
            ))
        db.commit()
    main = importlib.import_module("backend.main")
    with TestClient(main.app) as client:
        yield client
    engine.dispose()


def _headers(client, role="admin"):
    token = auth.create_access_token({"id": f"u-{role}", "username": role, "role": role})
    return {"Authorization": f"Bearer {token}"}


def test_knowledge_page_assets_and_management_navigation(api_client):
    page = api_client.get('/knowledge')
    assert page.status_code == 200
    assert '<title>知识资料' in page.text
    assert '/assets/knowledge-browser.js' in page.text
    assert '/assets/knowledge-ingest.js' in page.text
    assert '/assets/knowledge-recovery.js' in page.text
    assert 'id="refreshPending"' in page.text
    recovery_script = api_client.get('/assets/knowledge-recovery.js')
    assert recovery_script.status_code == 200 and 'class KnowledgeRecovery' in recovery_script.text
    assert 'id="ingestForm"' in page.text
    ingest_script = api_client.get('/assets/knowledge-ingest.js')
    assert ingest_script.status_code == 200 and 'class KnowledgeIngest' in ingest_script.text
    script = api_client.get('/assets/knowledge-browser.js')
    assert script.status_code == 200 and 'class KnowledgeBrowser' in script.text
    for path in ('/admin', '/settings'):
        assert 'href="/knowledge"' in api_client.get(path).text


def test_openapi_describes_typed_responses_and_error_statuses(api_client):
    spec = api_client.get("/openapi.json").json()
    for path, method, model in (
        ("/api/knowledge/diagnose", "get", "KnowledgeDiagnosisResponse"),
        ("/health", "get", "HealthResponse"),
    ):
        schema = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model}"}
    for path in ("/api/knowledge/ingest", "/api/knowledge/diagnose"):
        operation = spec["paths"][path]["post" if path.endswith("ingest") else "get"]
        assert "403" in operation["responses"]


def test_ingest_and_diagnose_flow(api_client):
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
    diagnosis = api_client.get("/api/knowledge/diagnose", params={"q": "导出", "top_k": 3}, headers=headers)
    assert diagnosis.status_code == 200 and source_id in diagnosis.json()["final_ids"]


@pytest.mark.parametrize("identity", [None, "customer"])
def test_admin_operations_reject_anonymous_and_customer(api_client, identity):
    headers = _headers(api_client, identity) if identity else {}
    for method, path, kwargs in (
        ("post", "/api/knowledge/ingest", {"json": {"title": "FAQ", "content": "正文"}}),
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


def test_health_only_reports_configuration(api_client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    response = api_client.get("/health")
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "ok"
    assert report["models"]["all_available"] is False
    assert {c["capability"] for c in report["models"]["capabilities"]} == {"chat", "embedding", "rerank"}
    assert all(not c["available"] for c in report["models"]["capabilities"])


def test_runtime_exposes_only_active_python_api_routes():
    main = importlib.import_module("backend.main")
    paths = main.app.openapi()["paths"]
    assert {path for path in paths if path.startswith("/api/")} == {
        "/api/knowledge/ingest",
        "/api/knowledge/ingest/file",
        "/api/knowledge/ingest/recover/{fingerprint}",
        "/api/knowledge/diagnose",
    }
    with TestClient(main.app) as client:
        assert client.post("/api/login", json={}).status_code == 404
        assert client.post("/api/chat", json={}).status_code == 404
        assert client.get("/api/tickets").status_code == 404
