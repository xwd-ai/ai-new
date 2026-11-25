from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import agent_internal


def test_agent_internal_uses_supplied_history_and_shared_token(monkeypatch):
    monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "test-agent-token")
    calls = []

    def generate(user_id, message, history):
        calls.append((user_id, message, history))
        summary = SimpleNamespace(to_dict=lambda: {"events": []})
        return "答复", [], False, "排查记录", (summary, None)

    monkeypatch.setattr(agent_internal, "enforce_chat_rate", lambda user_id: None)
    monkeypatch.setattr(agent_internal.chat, "generate_reply", generate)
    app = FastAPI()
    app.include_router(agent_internal.agent_router)
    with TestClient(app) as client:
        data = {
            "user_id": "u1", "message": "问题",
            "history": [{"role": "assistant", "content": "上轮答复"}],
        }
        assert client.post("/internal/agent/reply", json=data).status_code == 403
        assert calls == []
        response = client.post(
            "/internal/agent/reply", json=data, headers={"X-Agent-Token": "test-agent-token"},
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "答复"
    assert response.json()["tried_step"] == "排查记录"
    assert calls == [("u1", "问题", data["history"])]
