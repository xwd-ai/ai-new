import json
import math
import subprocess
import sys

import pytest


def evaluator():
    from backend.evaluation import retrieval
    return retrieval


def test_metrics_use_graded_gain_and_fixed_precision_denominator():
    metrics = evaluator().ranking_metrics(['b', 'x'], {'a':3, 'b':1}, {'a':'one', 'b':'two'}, 3)
    assert metrics['precision'] == pytest.approx(1/3)
    assert metrics['recall'] == 0.5 and metrics['mrr'] == 1
    assert metrics['ndcg'] == pytest.approx(1 / (7 + 1/math.log2(3)))
    assert metrics['aspect_recall'] == 0.5


def test_unanswerable_queries_are_reported_separately():
    metrics = evaluator().ranking_metrics(['x'], {}, {}, 3)
    assert metrics['recall'] is None and metrics['ndcg'] is None
    assert metrics['mrr'] == 0 and metrics['precision'] == 0
    assert metrics['false_positive'] == 1


def test_duplicate_ranked_ids_are_rejected():
    with pytest.raises(ValueError):
        evaluator().ranking_metrics(['a', 'a'], {'a':1}, {'a':'topic'}, 3)


def test_sample_runs_all_ablations_without_database_or_environment(monkeypatch):
    module = evaluator()
    from backend.services import knowledge
    monkeypatch.setattr(knowledge, '_all_docs', lambda: pytest.fail('评测不能读取数据库'))
    monkeypatch.setattr(knowledge, 'load_retrieval_config', lambda: pytest.fail('评测不能读取部署配置'))
    report = module.evaluate(module.load_dataset(module.DEFAULT_DATASET), top_k=2)
    assert set(report['variants']) == {'keyword', 'full', 'without_rrf', 'without_rerank', 'without_mmr'}
    assert report == module.evaluate(module.load_dataset(module.DEFAULT_DATASET), top_k=2)
    assert report['data_mode'] == 'synthetic_replay'
    assert len(report['dataset_sha256']) == 64
    assert report['query_count'] >= 5
    assert any(case['rankings']['full'] != case['rankings']['without_mmr'] for case in report['cases'])


@pytest.mark.parametrize('mutation', ['unknown_id', 'duplicate_vector', 'nan_score', 'duplicate_doc', 'ambiguous_passage'])
def test_invalid_dataset_rejected(mutation):
    module = evaluator()
    data = json.loads(module.DEFAULT_DATASET.read_text('utf-8'))
    query = data['queries'][0]
    if mutation == 'unknown_id': query['relevance']['missing'] = 3
    elif mutation == 'duplicate_vector': query['vector_ranking'] = ['export', 'export']
    elif mutation == 'nan_score': query['rerank_scores']['export'] = float('nan')
    elif mutation == 'duplicate_doc': data['documents'].append(data['documents'][0])
    else: data['documents'][1] = {**data['documents'][0], 'id':'export-copy'}
    with pytest.raises(ValueError):
        module.validate_dataset(data)


def test_cli_writes_readable_json(tmp_path):
    target = tmp_path / 'report.json'
    result = subprocess.run([sys.executable, '-m', 'backend.evaluation.retrieval', '--top-k', '2',
                             '--output', str(target)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads(target.read_text('utf-8'))
    assert report['top_k'] == 2
    assert 'full' in report['variants']


def test_labels_do_not_influence_rankings():
    module = evaluator()
    original = module.load_dataset(module.DEFAULT_DATASET)
    changed = original.model_dump()
    for query in changed['queries']:
        query['relevance'], query['groups'] = {}, {}
    first = module.evaluate(original, top_k=2)
    second = module.evaluate(module.validate_dataset(changed), top_k=2)
    assert [case['rankings'] for case in first['cases']] == [case['rankings'] for case in second['cases']]


def test_rrf_ablation_holds_truncated_candidate_pool_fixed(monkeypatch):
    module = evaluator()
    docs = [dict(id=str(i), title='queryword '+str(i), content='body '+str(i), kind='faq', keywords=[]) for i in range(80)]
    data = dict(data_mode='synthetic_replay', description='candidate boundary', documents=docs,
                queries=[dict(id='q', query='queryword', relevance={'1':1}, groups={'1':'topic'},
                              vector_ranking=[str(i) for i in reversed(range(80))],
                              rerank_scores={str(i):0.5 for i in range(80)})])
    seen = []
    original = module.knowledge.rerank_results
    def capture(query, results, **kwargs):
        seen.append({row['id'] for row in results})
        return original(query, results, **kwargs)
    monkeypatch.setattr(module.knowledge, 'rerank_results', capture)
    module.evaluate(module.validate_dataset(data), top_k=2)
    assert seen[0] == seen[1] and len(seen[0]) == 50


def test_late_relevant_hit_metrics():
    result = evaluator().ranking_metrics(['x', 'a'], {'a':3}, {'a':'topic'}, 2)
    assert result['mrr'] == 0.5
    assert result['ndcg'] == pytest.approx(1/math.log2(3))
