"""在固定人工标注与合成模型回放上比较检索组件，不估计真实模型质量。"""

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services import knowledge
from backend.services.retrieval_config import RetrievalConfig

DEFAULT_DATASET = Path(__file__).with_name('retrieval_sample.json')
Grade = Annotated[int, Field(strict=True, ge=0, le=3)]
Score = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class Document(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1)
    title: str
    content: str
    kind: str
    keywords: list[str]


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    relevance: dict[str, Grade]
    groups: dict[str, str]
    vector_ranking: list[str]
    rerank_scores: dict[str, Score]


class Dataset(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    data_mode: str
    description: str
    documents: list[Document] = Field(min_length=1)
    queries: list[Query] = Field(min_length=1)

    @model_validator(mode='after')
    def check_references(self):
        if self.data_mode != 'synthetic_replay':
            raise ValueError('只支持明确标记的 synthetic_replay 数据')
        ids = {d.id for d in self.documents}
        if len(ids) != len(self.documents) or len({q.id for q in self.queries}) != len(self.queries):
            raise ValueError('文档或问题编号重复')
        passages = [knowledge.build_passage(d.model_dump()) for d in self.documents]
        if len(set(passages)) != len(passages):
            raise ValueError('回放资料文本重复，无法无歧义匹配重排分数')
        for query in self.queries:
            if not set(query.relevance) <= ids or not set(query.vector_ranking) <= ids:
                raise ValueError('标注或向量排名引用了未知文档')
            if len(set(query.vector_ranking)) != len(query.vector_ranking):
                raise ValueError('向量排名含重复文档')
            if set(query.rerank_scores) != ids:
                raise ValueError('回放重排分数必须覆盖所有文档')
            relevant = {key for key, value in query.relevance.items() if value > 0}
            if set(query.groups) != relevant or any(not group.strip() for group in query.groups.values()):
                raise ValueError('每份相关文档必须有且仅有一个非空方面标注')
        return self


def validate_dataset(data: dict) -> Dataset:
    return Dataset.model_validate(data)


def load_dataset(path: Path) -> Dataset:
    return validate_dataset(json.loads(path.read_text(encoding='utf-8')))


def ranking_metrics(ranked: list[str], relevance: dict, groups: dict, k: int) -> dict:
    if type(k) is not int or k < 1 or len(set(ranked)) != len(ranked):
        raise ValueError('k 必须为正整数，排名不能重复')
    ranked = ranked[:k]
    relevant = {key for key, grade in relevance.items() if grade > 0}
    hits = [i for i, key in enumerate(ranked, 1) if key in relevant]
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum((2**grade - 1) / math.log2(i+1) for i, grade in enumerate(ideal, 1))
    dcg = sum((2**relevance.get(key, 0) - 1) / math.log2(i+1) for i, key in enumerate(ranked, 1))
    return {
        'precision': len(hits)/k,
        'recall': len(hits)/len(relevant) if relevant else None,
        'mrr': 1/hits[0] if hits else 0.0,
        'ndcg': dcg/idcg if idcg else None,
        'aspect_recall': len({groups[key] for key in ranked if key in relevant})/len(set(groups.values())) if relevant else None,
        'false_positive': int(bool(ranked)) if not relevant else None,
    }


@dataclass
class ReplayHit:
    index: int
    relevance_score: float


class ReplayReranker:
    def __init__(self, docs, scores):
        self.scores = {knowledge.build_passage(doc): scores[doc['id']] for doc in docs}

    def rerank(self, query, passages, *, top_n):
        # 只按预先提供的分数回放，不从 relevance 或 groups 生成排名。
        hits = [ReplayHit(i, self.scores[text]) for i, text in enumerate(passages)]
        return sorted(hits, key=lambda hit: -hit.relevance_score)[:top_n]


def evaluate(dataset: Dataset, *, top_k: int = 3) -> dict:
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError('top_k 必须为 1～20 的整数')
    cfg = RetrievalConfig()  # 不加载环境变量或管理页覆盖值。
    docs = [d.model_dump() for d in dataset.documents]
    by_id = {d['id']: d for d in docs}
    cases = []
    for query in dataset.queries:
        scores = knowledge._normalize(knowledge._bm25_scores(knowledge.tokenize(query.query), docs))
        keyword = [{**doc, 'score': score, 'match_type':'keyword'} for doc, score in zip(docs, scores) if score > 0]
        keyword.sort(key=lambda item: -item['score'])
        depth = cfg.prefetch_depth(top_k)
        keyword = keyword[:depth]
        vector = [{**by_id[key], 'score': 1/(i+1), 'match_type':'vector'}
                  for i, key in enumerate(query.vector_ranking[:depth])]
        hybrid = knowledge.fuse_rrf(vector_results=vector, keyword_results=keyword, top_k=depth, config=cfg) if vector else keyword
        reranker = ReplayReranker(docs, query.rerank_scores)
        reranked = knowledge.rerank_results(query.query, hybrid, rerank_provider=reranker, top_k=depth, threshold=0.0, config=cfg)
        # 保留同一候选集合：无 RRF 时按向量优先的首次出现顺序排列，
        # 统一召回分，避免把删掉向量候选误当作取消融合的影响。
        union = {item['id']: item for item in reversed(vector + keyword)}
        selected_ids = {item['id'] for item in hybrid}
        no_rrf_pool = [{**union[key], 'score':1.0} for key in dict.fromkeys(item['id'] for item in vector + keyword) if key in selected_ids]
        no_rrf = knowledge.rerank_results(query.query, no_rrf_pool, rerank_provider=reranker, top_k=depth, threshold=0.0, config=cfg)
        candidates = {
            'keyword': keyword[:top_k],
            'full': knowledge.select_mmr(reranked, top_k, lambda_=cfg.mmr_lambda),
            'without_rrf': knowledge.select_mmr(no_rrf, top_k, lambda_=cfg.mmr_lambda),
            'without_rerank': knowledge.select_mmr(hybrid, top_k, lambda_=cfg.mmr_lambda),
            'without_mmr': reranked[:top_k],
        }
        rankings = {name: [item['id'] for item in items] for name, items in candidates.items()}
        cases.append({'id':query.id, 'query':query.query, 'rankings':rankings,
                      'metrics':{name:ranking_metrics(ids, query.relevance, query.groups, top_k) for name, ids in rankings.items()}})
    names = list(cases[0]['rankings'])
    variants = {}
    for name in names:
        summary = {}
        for metric in cases[0]['metrics'][name]:
            values = [case['metrics'][name][metric] for case in cases if case['metrics'][name][metric] is not None]
            summary[metric] = sum(values)/len(values) if values else None
        variants[name] = summary
    canonical = json.dumps(dataset.model_dump(), sort_keys=True, ensure_ascii=False).encode('utf-8')
    return {'data_mode':dataset.data_mode, 'description':dataset.description,
            'dataset_sha256':hashlib.sha256(canonical).hexdigest(), 'top_k':top_k,
            'query_count':len(cases), 'answerable_count':sum(any(v>0 for v in q.relevance.values()) for q in dataset.queries),
            'config':{'rrf_k':cfg.rrf_k, 'vector_weight':cfg.vector_weight, 'keyword_weight':cfg.keyword_weight,
                      'prefetch_depth':cfg.prefetch_depth(top_k), 'mmr_lambda':cfg.mmr_lambda, 'rerank_threshold':0.0},
            'definitions':{'keyword':'BM25 only', 'full':'RRF + replay rerank + MMR',
                           'without_rrf':'hold full candidate pool fixed; vector-first stable order; constant recall score; rerank + MMR',
                           'without_rerank':'RRF + MMR', 'without_mmr':'RRF + replay rerank; truncate'},
            'variants':variants, 'delta_vs_full':{name:{metric:(value-variants['full'][metric]) if value is not None else None
                                                     for metric,value in summary.items()} for name,summary in variants.items()},
            'cases':cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    parser.add_argument('--top-k', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = evaluate(load_dataset(args.dataset), top_k=args.top_k)
        rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.output:
            args.output.write_text(rendered, encoding='utf-8')
        else:
            print(rendered, end='')
    except (ValueError, OSError) as error:
        parser.exit(2, f'评测失败：{error}\n')


if __name__ == '__main__':
    main()
