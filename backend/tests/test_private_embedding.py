from backend import providers
from backend.services import knowledge


def test_e5_prefixes_batches_and_reuses_document_vectors(monkeypatch):
    provider = providers.OpenAICompatEmbeddingProvider(
        api_key="test-key", base_url="https://127.0.0.1:18443/v1",
        model="intfloat/multilingual-e5-small", e5=True,
    )
    requests = []

    def embed_batch(texts, model):
        requests.append((list(texts), model))
        return [{"index": index, "embedding": [1.0, 0.0]} for index in reversed(range(len(texts)))]

    monkeypatch.setattr(provider, "_request_private", embed_batch)
    assert provider.embed("数据库") == [1.0, 0.0]
    documents = [f"Document {i}" for i in range(17)]
    assert len(provider.embed_many(documents)) == 17
    assert provider.embed_many(documents[:2]) == [[1.0, 0.0]] * 2
    assert [len(texts) for texts, _ in requests] == [1, 16, 1]
    assert requests[0][0] == ["query: 数据库"]
    assert requests[1][0][0] == "passage: Document 0"


def test_chat_uses_configured_embedding_and_degrades_when_unavailable(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    provider = object()
    monkeypatch.setattr(providers, "build_embedding_provider", lambda: provider)
    monkeypatch.setattr(knowledge, "search_knowledge_hybrid", lambda q, *, top_k, embedding_provider: [embedding_provider])
    monkeypatch.setattr(knowledge, "search_knowledge", lambda q, *, top_k: ["keyword"])
    assert knowledge.search_for_chat("问题") == [provider]

    def missing_certificate():
        raise OSError("unavailable CA file")

    monkeypatch.setattr(providers, "build_embedding_provider", missing_certificate)
    assert knowledge.search_for_chat("问题") == ["keyword"]
