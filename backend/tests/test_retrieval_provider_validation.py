from types import SimpleNamespace

import pytest

from backend.services import knowledge
from backend.services.retrieval_config import RetrievalConfig

DOCS = [dict(id='a', title='alpha', content='guide', keywords=[], kind='faq', score=0.9),
        dict(id='b', title='beta', content='manual', keywords=[], kind='manual', score=0.7)]


class Embedding:
    def __init__(self, query, batch): self.query, self.batch = query, batch
    def embed(self, query): return self.query
    def embed_many(self, texts): return self.batch


@pytest.mark.parametrize('query,batch', [
    (None, [[1,0],[1,0]]), ('bad', [[1,0],[1,0]]), ([True,0], [[1,0],[1,0]]),
    ([float('nan'),1], [[1,0],[1,0]]), ([1,0], None), ([1,0], [[1,0]]),
    ([1,0], [[1,0],[1]]), ([1,0], [[1,0],['x',0]]),
    ([1,0], [[1,0],[float('inf'),0]]), ([1,0], [[1,0],[0,0]]),
])
def test_invalid_embeddings_discard_entire_vector_lane(monkeypatch, query, batch):
    monkeypatch.setattr(knowledge, '_all_docs', lambda: DOCS)
    detail = {}
    assert knowledge.search_knowledge_by_vector('q', embedding_provider=Embedding(query,batch), diagnostics=detail) == []
    assert detail['skipped'] == 'invalid_response'


def test_cosine_does_not_overflow_for_large_finite_values(monkeypatch):
    monkeypatch.setattr(knowledge, '_all_docs', lambda: DOCS)
    hits = knowledge.search_knowledge_by_vector('q', embedding_provider=Embedding([1e308,1e308], [[1e308,1e308],[1e308,0]]))
    assert hits[0]['id'] == 'a' and hits[0]['score'] == pytest.approx(1)
    assert hits[1]['score'] == pytest.approx(0.7071)


@pytest.mark.parametrize('hits', [
    'bad', {}, [None], [SimpleNamespace(index=True, relevance_score=0.9)],
    [SimpleNamespace(index=0.5, relevance_score=0.9)],
    [SimpleNamespace(index=-1, relevance_score=0.9)],
    [SimpleNamespace(index=2, relevance_score=0.9)],
    [SimpleNamespace(index=0, relevance_score='0.9')],
    [SimpleNamespace(index=0, relevance_score=True)],
    [SimpleNamespace(index=0, relevance_score=float('nan'))],
    [SimpleNamespace(index=0, relevance_score=float('inf'))],
    [SimpleNamespace(index=0, relevance_score=0.9), SimpleNamespace(index=0, relevance_score=0.8)],
])
def test_invalid_rerank_response_keeps_original_candidates(hits):
    class Provider:
        def rerank(self, *args, **kwargs): return hits
    results, diag = knowledge.rerank_results('q', DOCS, rerank_provider=Provider(),
                      config=RetrievalConfig(), with_diagnostics=True)
    assert results == DOCS
    assert diag['skipped'] == 'invalid_response'
    assert DOCS[0]['score'] == 0.9


def test_valid_partial_rerank_and_finite_score_clamping():
    class Provider:
        def rerank(self, *args, **kwargs): return [SimpleNamespace(index=1, relevance_score=1.2)]
    results = knowledge.rerank_results('q', DOCS, rerank_provider=Provider(), config=RetrievalConfig())
    assert len(results) == 1 and results[0]['id'] == 'b'
    assert results[0]['rerank_score'] == 1


def test_diagnostic_keeps_keyword_lane_for_bad_vectors(monkeypatch):
    monkeypatch.setattr(knowledge, '_all_docs', lambda: DOCS)
    result = knowledge.diagnose('alpha', embedding_provider=Embedding([1,0], [[1],[1]]), config=RetrievalConfig())
    assert result['final_ids'] == ['a']
    assert result['stages'][1]['detail']['skipped'] == 'invalid_response'


def test_lazy_provider_exception_is_safe():
    class BrokenList(list):
        def __iter__(self): raise RuntimeError('private-provider-secret')
    class Provider:
        def rerank(self, *args, **kwargs): return BrokenList([1])
    results, diag = knowledge.rerank_results('q', DOCS, rerank_provider=Provider(),
                      config=RetrievalConfig(), with_diagnostics=True)
    assert results == DOCS and diag['skipped'] == 'invalid_response'
    assert 'private' not in str(diag)


def test_vector_diagnostics_can_be_reused_without_stale_failure(monkeypatch):
    monkeypatch.setattr(knowledge, '_all_docs', lambda: DOCS)
    detail = {'skipped':'invalid_response'}
    result = knowledge.search_knowledge_by_vector('q', embedding_provider=Embedding([1,0],[[1,0],[0,1]]), diagnostics=detail)
    assert [r['id'] for r in result] == ['a']
    assert detail == {}


def test_vector_provider_exception_hides_original_text(monkeypatch):
    monkeypatch.setattr(knowledge, '_all_docs', lambda: DOCS)
    class Broken:
        def embed(self, query): raise RuntimeError('private-provider-secret')
    detail = {}
    assert knowledge.search_knowledge_by_vector('q', embedding_provider=Broken(), diagnostics=detail) == []
    assert detail == {'skipped':'model_error'}


def test_rerank_index_refers_to_nonempty_passages():
    docs = [dict(id='blank', title='', content='', keywords=[], kind='faq', score=1), *DOCS]
    class Provider:
        def rerank(self, query, passages, **kwargs):
            assert len(passages) == 2
            return [SimpleNamespace(index=1, relevance_score=0.9)]
    result = knowledge.rerank_results('q', docs, rerank_provider=Provider(), config=RetrievalConfig())
    assert [r['id'] for r in result] == ['b']


def test_zero_query_vector_has_no_vector_results(monkeypatch):
    monkeypatch.setattr(knowledge, '_all_docs', lambda: DOCS)
    detail = {}
    assert knowledge.search_knowledge_by_vector('q', embedding_provider=Embedding([0,0],[[1,0],[0,1]]), diagnostics=detail) == []
    assert detail['skipped'] == 'invalid_response'
