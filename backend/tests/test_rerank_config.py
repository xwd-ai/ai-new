from types import SimpleNamespace

import pytest

from backend.services import knowledge
from backend.services.retrieval_config import RetrievalConfig
from backend.tests.test_api_contracts import api_client, _headers


DOCS = [dict(id='a', title='alpha', content='same words', kind='faq', score=0.9),
        dict(id='b', title='beta', content='different', kind='rule', score=0.3)]


class Provider:
    def __init__(self, scores):
        self.scores = scores
        self.count = 0

    def rerank(self, query, passages, *, top_n):
        self.count = len(passages)
        return [SimpleNamespace(index=i, relevance_score=score) for i, score in enumerate(self.scores[:top_n])]


def test_candidate_limit_and_composite_weights_are_applied():
    provider = Provider([0.6, 0.9])
    cfg = RetrievalConfig(max_rerank_candidates=1, composite_rerank=1, composite_recall=0, composite_source=0)
    results, diag = knowledge.rerank_results('q', DOCS, rerank_provider=provider, config=cfg, with_diagnostics=True)
    assert provider.count == 1
    assert results[0]['score'] == 0.6
    assert diag['config']['max_rerank_candidates'] == 1


def test_source_weight_override_changes_order():
    cfg = RetrievalConfig(composite_rerank=0, composite_recall=0, composite_source=1,
                          source_weights={'faq':0.1, 'rule':0.9})
    results = knowledge.rerank_results('q', DOCS, rerank_provider=Provider([1, 1]), config=cfg)
    assert [r['id'] for r in results] == ['b', 'a']


def test_threshold_defaults_to_config_and_explicit_zero_overrides():
    cfg = RetrievalConfig(threshold=0.9, degrade_floor=0.8, degrade_factor=0.9, fallback_min_score=0.7)
    results, diag = knowledge.rerank_results('q', DOCS, rerank_provider=Provider([0.5, 0.4]), config=cfg, with_diagnostics=True)
    assert results == []
    assert diag['effective_threshold'] == pytest.approx(0.81)
    assert diag['config']['threshold'] == 0.9
    results, diag = knowledge.rerank_results('q', DOCS, rerank_provider=Provider([0.5, 0.4]), config=cfg, threshold=0, with_diagnostics=True)
    assert len(results) == 2 and diag['config']['threshold'] == 0
    assert cfg.threshold == 0.9


def test_diagnosis_uses_one_snapshot_even_when_provider_changes_config(monkeypatch):
    cfg = RetrievalConfig(threshold=0.9, mmr_lambda=0.8, max_rerank_candidates=1)
    calls = []
    monkeypatch.setattr(knowledge, 'load_retrieval_config', lambda: calls.append(1) or cfg)
    monkeypatch.setattr(knowledge, 'search_knowledge', lambda *args, **kwargs: DOCS)
    monkeypatch.setattr(knowledge, 'search_knowledge_by_vector', lambda *args, **kwargs: [])
    class MutatingProvider(Provider):
        def rerank(self, *args, **kwargs):
            cfg.threshold, cfg.mmr_lambda, cfg.max_rerank_candidates = 0, 0, 200
            return super().rerank(*args, **kwargs)
    result = knowledge.diagnose('q', rerank_provider=MutatingProvider([0.95]))
    assert len(calls) == 1
    assert result['config']['threshold'] == 0.9
    assert result['config']['mmr_lambda'] == 0.8
    assert result['stages'][3]['detail']['config'] == result['config']


@pytest.mark.parametrize('mode', ['missing', 'failure', 'empty_input'])
def test_skipped_paths_expose_effective_snapshot(mode):
    class Broken:
        def rerank(self, *args, **kwargs):
            raise RuntimeError('private')
    result, diag = knowledge.rerank_results('q', [] if mode == 'empty_input' else DOCS,
                       rerank_provider=None if mode == 'missing' else Broken(),
                       config=RetrievalConfig(threshold=0.5), with_diagnostics=True)
    assert diag['config']['threshold'] == 0.5
    assert 'private' not in str(diag)


def test_environment_values_reach_actual_rerank(monkeypatch, tmp_path):
    from backend.services import retrieval_config
    monkeypatch.setattr(retrieval_config, '_settings_path', lambda: tmp_path/'absent.json')
    for key, value in {'RETRIEVAL_MAX_RERANK_CANDIDATES':'1', 'RETRIEVAL_COMPOSITE_RERANK':'1',
                       'RETRIEVAL_COMPOSITE_RECALL':'0', 'RETRIEVAL_COMPOSITE_SOURCE':'0',
                       'RETRIEVAL_THRESHOLD':'0.8', 'RETRIEVAL_DEGRADE_FLOOR':'0.7',
                       'RETRIEVAL_DEGRADE_FACTOR':'0.9', 'RETRIEVAL_FALLBACK_MIN_SCORE':'0.6'}.items():
        monkeypatch.setenv(key, value)
    provider = Provider([0.65])
    results, diag = knowledge.rerank_results('q', DOCS, rerank_provider=provider, with_diagnostics=True)
    assert provider.count == 1 and diag['fallback']
    assert results[0]['score'] == 0.65


def test_rerank_mmr_wrapper_uses_only_one_configuration(monkeypatch):
    calls = []
    cfg = RetrievalConfig(max_rerank_candidates=1, mmr_lambda=0.4)
    monkeypatch.setattr(knowledge, 'load_retrieval_config', lambda: calls.append(1) or cfg)
    provider = Provider([1, 1])
    assert len(knowledge.rerank_with_mmr('q', DOCS, rerank_provider=provider)) == 1
    assert len(calls) == 1


def test_diagnostic_override_is_consistent_without_changing_caller(monkeypatch):
    monkeypatch.setattr(knowledge, 'search_knowledge', lambda *args, **kwargs: DOCS)
    monkeypatch.setattr(knowledge, 'search_knowledge_by_vector', lambda *args, **kwargs: [])
    cfg = RetrievalConfig(threshold=0.8)
    result = knowledge.diagnose('q', config=cfg, threshold=0)
    assert result['config']['threshold'] == result['stages'][3]['detail']['threshold'] == 0
    assert cfg.threshold == 0.8


def test_nested_source_weights_are_snapshotted():
    cfg = RetrievalConfig(composite_rerank=0, composite_recall=0, composite_source=1,
                          source_weights={'faq':0.2, 'rule':0.8})
    class MutatingProvider(Provider):
        def rerank(self, *args, **kwargs):
            cfg.source_weights['faq'] = 1
            return super().rerank(*args, **kwargs)
    result, diag = knowledge.rerank_results('q', DOCS, rerank_provider=MutatingProvider([1,1]),
                                           config=cfg, with_diagnostics=True)
    assert [r['id'] for r in result] == ['b', 'a']
    assert diag['config']['source_weights']['faq'] == 0.2


def test_default_composite_score_preserves_previous_behavior():
    result = knowledge.composite_score(DOCS[0], 0.5, config=RetrievalConfig())
    assert result == pytest.approx(0.6*0.5 + 0.3*0.9 + 0.1*0.8)


def test_http_diagnostic_reports_configuration_but_does_not_filter_without_provider(api_client, monkeypatch, tmp_path):
    from backend.services import retrieval_config
    monkeypatch.setattr(retrieval_config, '_settings_path', lambda: tmp_path/'absent.json')
    monkeypatch.setenv('RETRIEVAL_THRESHOLD', '0.8')
    monkeypatch.setenv('RETRIEVAL_MAX_RERANK_CANDIDATES', '1')
    headers = _headers(api_client)
    created = api_client.post('/api/knowledge/ingest', headers=headers,
                              json={'title':'export', 'content':'CSV export guide'}).json()
    result = api_client.get('/api/knowledge/diagnose', headers=headers, params={'q':'export'}).json()
    assert result['config']['threshold'] == 0.8
    assert result['config']['max_rerank_candidates'] == 1
    assert result['stages'][3]['detail']['config'] == result['config']
    assert result['stages'][3]['detail']['skipped'] == 'no_provider'
    assert result['final_ids'] == [created['knowledge_id']]
