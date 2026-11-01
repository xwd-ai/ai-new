from types import SimpleNamespace

import pytest

from backend.services import knowledge
from backend.services.retrieval_config import RetrievalConfig


DOCS = [dict(id='recall-first', title='alpha recall', content='guide', kind='faq', score=1.0),
        dict(id='relevance-first', title='alpha relevant', content='manual', kind='manual', score=0.0)]


class Provider:
    def __init__(self, scores): self.scores = scores
    def rerank(self, query, passages, *, top_n):
        return [SimpleNamespace(index=i, relevance_score=score) for i, score in enumerate(self.scores)]


def config():
    return RetrievalConfig(**dict(threshold=0.9, degrade_floor=0.3, degrade_factor=0.7,
                                   fallback_min_score=0.15, composite_rerank=0.1,
                                   composite_recall=0.9, composite_source=0))


@pytest.mark.parametrize('scores', [[0.1, 0.2], [0.2, 0.3]])
def test_fallback_selects_highest_model_relevance_not_composite_first(scores):
    result, diag = knowledge.rerank_results('alpha', DOCS, rerank_provider=Provider(scores),
                         config=config(), with_diagnostics=True)
    assert [item['id'] for item in result] == ['relevance-first']
    assert diag['fallback'] and diag['degraded']
    assert diag['effective_threshold'] == pytest.approx(0.63)
    assert diag['fallback_candidate_id'] == 'relevance-first'
    assert diag['fallback_candidate_score'] == scores[1]
    assert diag['fallback_min_score'] == 0.15
    assert DOCS[0]['score'] == 1.0 and 'rerank_score' not in DOCS[0]


def test_best_model_score_below_floor_stays_empty_and_explains_rejection():
    result, diag = knowledge.rerank_results('alpha', DOCS, rerank_provider=Provider([0.1,0.12]),
                         config=config(), with_diagnostics=True)
    assert result == [] and not diag['fallback']
    assert diag['fallback_candidate_id'] == 'relevance-first'
    assert diag['fallback_candidate_score'] == 0.12 < diag['fallback_min_score']


def test_equal_model_scores_keep_existing_composite_order():
    result, diag = knowledge.rerank_results('alpha', DOCS, rerank_provider=Provider([0.2,0.2]),
                         config=config(), with_diagnostics=True)
    assert [item['id'] for item in result] == ['recall-first']
    assert diag['fallback']


@pytest.mark.parametrize('scores,degraded', [([0.95,0.99],False), ([0.65,0.7],True)])
def test_passing_candidates_keep_composite_order(scores, degraded):
    result, diag = knowledge.rerank_results('alpha', DOCS, rerank_provider=Provider(scores),
                         config=config(), with_diagnostics=True)
    assert [item['id'] for item in result] == ['recall-first','relevance-first']
    assert diag['degraded'] == degraded and not diag['fallback']
    assert 'fallback_candidate_id' not in diag


def test_threshold_zero_and_missing_provider_do_not_invent_fallback():
    for provider in (None, Provider([0.1,0.2])):
        result, diag = knowledge.rerank_results('alpha', DOCS, rerank_provider=provider,
                             config=config(), threshold=0, with_diagnostics=True)
        assert len(result) == 2 and not diag.get('fallback', False)
        assert 'fallback_candidate_id' not in diag


def test_diagnosis_and_mmr_wrapper_keep_selected_fallback(monkeypatch):
    monkeypatch.setattr(knowledge, 'search_knowledge', lambda *args, **kwargs: DOCS)
    monkeypatch.setattr(knowledge, 'search_knowledge_by_vector', lambda *args, **kwargs: [])
    diagnosis = knowledge.diagnose('alpha', top_k=1, config=config(), rerank_provider=Provider([0.1,0.2]))
    assert diagnosis['final_ids'] == ['relevance-first']
    detail = diagnosis['stages'][3]['detail']
    assert detail['fallback_candidate_id'] == diagnosis['final_ids'][0]
    assert detail['config'] == diagnosis['config']
    assert detail['returned'] == 1
    result = knowledge.rerank_with_mmr('alpha', DOCS, top_k=1, config=config(), rerank_provider=Provider([0.1,0.2]))
    assert [item['id'] for item in result] == ['relevance-first']


def test_model_score_exactly_at_fallback_floor_is_accepted():
    result, diag = knowledge.rerank_results('alpha', DOCS, rerank_provider=Provider([0.1,0.15]),
                         config=config(), with_diagnostics=True)
    assert [item['id'] for item in result] == ['relevance-first']
    assert diag['fallback']
