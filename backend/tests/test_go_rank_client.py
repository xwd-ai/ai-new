"""Go 检索后处理（/rank）客户端与服务层接入测试。

主线仍是「远程优先、失败降级」，重点锁住三条边界：

- 未配置 ``GO_RETRIEVAL_URL`` 时完全不发请求，后处理走纯本地实现；
- 配置了但连接失败 / 非法 JSON / 结构不完整，一律按不可用降级；
- **有重排模型时不走远程**：模型客户端与密钥在 Python 侧，
  这条路径无法远程化，硬转过去只会丢掉重排效果。
"""

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from backend.services import go_client, knowledge

REMOTE_URL = "http://127.0.0.1:8081"


class _CapturingOpener:
    """替换 go_client._opener：记录请求对象，便于断言实际发出的请求体。"""

    def __init__(self, payload=b"{}", error=None):
        self.payload = payload
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(self.payload)


def _docs():
    """三条候选，分数降序，覆盖 rule / faq / manual 三种来源权重。"""
    return [
        {
            "id": "k-1",
            "title": "导出失败",
            "kind": "faq",
            "content": "专业版导出失败时先检查授权是否仍在有效期内",
            "keywords": ["导出", "失败"],
            "score": 0.9,
            "match_type": "keyword",
        },
        {
            "id": "k-2",
            "title": "导出格式",
            "kind": "manual",
            "content": "导出支持 PDF 与 Excel 两种格式，Excel 保留表格样式",
            "keywords": ["导出", "格式"],
            "score": 0.8,
            "match_type": "keyword",
        },
        {
            "id": "k-3",
            "title": "授权激活",
            "kind": "rule",
            "content": "激活码在设置页输入后立即生效，无需重启客户端",
            "keywords": ["授权", "激活"],
            "score": 0.6,
            "match_type": "keyword",
        },
    ]


# ---------------------------------------------------------------------------
# 客户端层：配置、降级与请求体
# ---------------------------------------------------------------------------


def test_rank_without_config_never_calls_network(monkeypatch):
    monkeypatch.delenv("GO_RETRIEVAL_URL", raising=False)
    opener = _CapturingOpener()
    monkeypatch.setattr(go_client, "_opener", opener)

    assert go_client.rank("导出失败", _docs(), top_k=2) is None
    assert opener.requests == []


def test_rank_degrades_on_network_failure(monkeypatch):
    monkeypatch.setenv("GO_RETRIEVAL_URL", REMOTE_URL)
    monkeypatch.setattr(
        go_client, "_opener", _CapturingOpener(error=urllib.error.URLError("refused"))
    )

    assert go_client.rank("导出失败", _docs(), top_k=2) is None


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"[]", b"{}", b'{"results": "oops"}', b'{"results": null}'],
)
def test_rank_degrades_on_invalid_payload(monkeypatch, payload):
    monkeypatch.setenv("GO_RETRIEVAL_URL", REMOTE_URL)
    monkeypatch.setattr(go_client, "_opener", _CapturingOpener(payload=payload))

    assert go_client.rank("导出失败", _docs(), top_k=2) is None


def test_rank_skips_network_for_empty_results(monkeypatch):
    monkeypatch.setenv("GO_RETRIEVAL_URL", REMOTE_URL)
    opener = _CapturingOpener()
    monkeypatch.setattr(go_client, "_opener", opener)

    assert go_client.rank("导出失败", [], top_k=2) is None
    assert opener.requests == []


def test_rank_posts_options_and_config(monkeypatch):
    monkeypatch.setenv("GO_RETRIEVAL_URL", REMOTE_URL)
    payload = json.dumps({"results": [{"id": "k-1", "score": 0.7}]}).encode("utf-8")
    opener = _CapturingOpener(payload=payload)
    monkeypatch.setattr(go_client, "_opener", opener)

    got = go_client.rank(
        "导出失败",
        _docs(),
        top_k=2,
        threshold=0.3,
        lambda_=0.5,
        config={"rrf_k": 60, "mmr_lambda": 0.7},
    )

    assert got == [{"id": "k-1", "score": 0.7}]
    body = json.loads(opener.requests[0].data.decode("utf-8"))
    assert body["query"] == "导出失败"
    assert body["top_k"] == 2
    assert body["threshold"] == 0.3
    assert body["lambda"] == 0.5
    assert body["config"] == {"rrf_k": 60, "mmr_lambda": 0.7}
    assert [item["id"] for item in body["results"]] == ["k-1", "k-2", "k-3"]


def test_rank_omits_unset_options(monkeypatch):
    """未指定的可选参数不写进请求体，让 Go 侧沿用配置默认值。"""
    monkeypatch.setenv("GO_RETRIEVAL_URL", REMOTE_URL)
    opener = _CapturingOpener(payload=json.dumps({"results": []}).encode("utf-8"))
    monkeypatch.setattr(go_client, "_opener", opener)

    go_client.rank("导出失败", _docs(), top_k=2)

    body = json.loads(opener.requests[0].data.decode("utf-8"))
    assert "threshold" not in body
    assert "lambda" not in body
    assert "config" not in body


# ---------------------------------------------------------------------------
# 服务层：远程优先、降级一致、模型路径不远程化
# ---------------------------------------------------------------------------


def test_rerank_with_mmr_prefers_remote(monkeypatch):
    """配置了检索服务且无重排模型时，直接采用 Go 的后处理结果。"""
    remote = [{"id": "k-3", "score": 0.55}, {"id": "k-1", "score": 0.54}]
    calls = []

    def fake_rank(query, results, **kwargs):
        calls.append({"query": query, "count": len(results), **kwargs})
        return remote

    monkeypatch.setenv("GO_RETRIEVAL_URL", REMOTE_URL)
    monkeypatch.setattr(go_client, "rank", fake_rank)

    got = knowledge.rerank_with_mmr("导出失败", _docs(), top_k=2)

    assert got == remote
    assert len(calls) == 1
    assert calls[0]["query"] == "导出失败"
    assert calls[0]["count"] == 3
    assert calls[0]["top_k"] == 2


def test_rerank_with_mmr_falls_back_to_local_result(monkeypatch):
    """远程不可用时退回本地，结果必须与「从未配置远程」完全一致。"""
    docs = _docs()

    monkeypatch.delenv("GO_RETRIEVAL_URL", raising=False)
    local = knowledge.rerank_with_mmr("导出失败", docs, top_k=2)

    monkeypatch.setenv("GO_RETRIEVAL_URL", REMOTE_URL)
    monkeypatch.setattr(go_client, "rank", lambda *args, **kwargs: None)
    degraded = knowledge.rerank_with_mmr("导出失败", docs, top_k=2)

    assert degraded == local
    assert [r["id"] for r in degraded]


def test_rerank_with_mmr_skips_remote_without_config(monkeypatch):
    monkeypatch.delenv("GO_RETRIEVAL_URL", raising=False)
    monkeypatch.setattr(
        go_client, "rank", lambda *args, **kwargs: pytest.fail("未配置时不应调用远程")
    )

    got = knowledge.rerank_with_mmr("导出失败", _docs(), top_k=2)

    assert len(got) == 2


def test_rerank_with_mmr_keeps_model_path_local(monkeypatch):
    """有重排模型时模型调用必须留在 Python，不能为了统一入口转给 Go。"""
    monkeypatch.setenv("GO_RETRIEVAL_URL", REMOTE_URL)
    monkeypatch.setattr(
        go_client,
        "rank",
        lambda *args, **kwargs: pytest.fail("有重排模型时不应走 Go"),
    )

    class Provider:
        def rerank(self, query, passages, top_n=None):
            return [
                SimpleNamespace(index=i, relevance_score=0.5 + i * 0.01)
                for i in range(len(passages))
            ]

    got = knowledge.rerank_with_mmr(
        "导出失败", _docs(), rerank_provider=Provider(), top_k=2
    )

    assert len(got) == 2
    assert all("rerank_score" in item for item in got)
