"""检索设置持久化与实际参数传递。"""

import json
import subprocess
import sys

import pytest

from backend.schemas.retrieval import RetrievalSettings
from backend.services import knowledge, retrieval_config
from backend.tests.test_api_contracts import api_client, _headers


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "retrieval_settings.json"
    monkeypatch.setattr(retrieval_config, "_settings_path", lambda: path, raising=False)
    return path


def payload():
    return dict(prefetch_floor=12, prefetch_multiplier=2, rrf_k=40,
                vector_weight=0.6, keyword_weight=0.4, mmr_lambda=0.25)


def test_save_reload_and_environment_precedence(settings_file, monkeypatch):
    monkeypatch.setenv("RETRIEVAL_PREFETCH_FLOOR", "99")
    assert retrieval_config.current_settings().prefetch_floor == 99
    assert retrieval_config.save_settings(RetrievalSettings(**payload())).model_dump() == payload()
    assert json.loads(settings_file.read_text("utf-8")) == payload()
    assert retrieval_config.load_retrieval_config().prefetch_depth(3) == 12
    code = (
        "from pathlib import Path; from backend.services import retrieval_config as c; "
        f"c._settings_path=lambda: Path({str(settings_file)!r}); "
        "assert c.load_retrieval_config().mmr_lambda == 0.25"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_zero_environment_weights_get_usable_defaults(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_VECTOR_WEIGHT", "0")
    monkeypatch.setenv("RETRIEVAL_KEYWORD_WEIGHT", "0")
    settings = retrieval_config.current_settings()
    assert settings.vector_weight + settings.keyword_weight > 0


@pytest.mark.parametrize("raw", ['{broken', '[]', '{"mmr_lambda": "NaN"}'])
def test_invalid_local_settings_fall_back_to_environment(settings_file, monkeypatch, raw):
    settings_file.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("RETRIEVAL_MMR_LAMBDA", "0.8")
    assert retrieval_config.load_retrieval_config().mmr_lambda == 0.8


def test_write_failure_preserves_settings_file(settings_file, monkeypatch):
    settings = RetrievalSettings(**payload())
    retrieval_config.save_settings(settings)
    before = settings_file.read_bytes()

    def fail(*args):
        raise PermissionError("private filesystem detail")

    monkeypatch.setattr(retrieval_config.os, "replace", fail)
    with pytest.raises(PermissionError):
        retrieval_config.save_settings(settings)
    assert settings_file.read_bytes() == before


def test_saved_settings_reach_diagnosis_and_explicit_config_mmr(api_client, monkeypatch):
    headers = _headers(api_client)
    retrieval_config.save_settings(RetrievalSettings(**payload()))
    seen = {}
    original = knowledge.select_mmr
    def capture(results, top_k, **kwargs):
        seen.update(kwargs)
        return original(results, top_k, **kwargs)
    monkeypatch.setattr(knowledge, "select_mmr", capture)
    result = api_client.get("/api/knowledge/diagnose", headers=headers, params={"q": "导出"})
    assert result.status_code == 200
    assert result.json()["prefetch_depth"] == 12
    assert seen["lambda_"] == 0.25
    knowledge.diagnose("导出", config=retrieval_config.RetrievalConfig(mmr_lambda=0.9))
    assert seen["lambda_"] == 0.9


def test_saved_fusion_weights_change_actual_order():
    data = payload()
    data.update(vector_weight=0, keyword_weight=1)
    retrieval_config.save_settings(RetrievalSettings(**data))
    a, b = {"id": "a"}, {"id": "b"}
    hits = knowledge.fuse_rrf(vector_results=[a, b], keyword_results=[b, a])
    assert [hit["id"] for hit in hits] == ["b", "a"]


def test_saved_mmr_setting_changes_actual_selection():
    data = payload()
    docs = [
        {"id": "a", "title": "alpha beta", "score": 1.0},
        {"id": "b", "title": "alpha beta", "score": 0.99},
        {"id": "c", "title": "gamma delta", "score": 0.7},
    ]
    for weight, expected in ((1, ["a", "b"]), (0, ["a", "c"])):
        data["mmr_lambda"] = weight
        retrieval_config.save_settings(RetrievalSettings(**data))
        assert [hit["id"] for hit in knowledge.select_mmr(docs, 2)] == expected


def test_settings_page_available_and_linked(api_client):
    response = api_client.get("/settings")
    assert response.status_code == 200
    assert "检索设置" in response.text
    assert "/settings" in api_client.get("/admin").text
