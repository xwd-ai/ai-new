"""知识库检索。

当前实现为关键词检索（BM25），后续接入向量召回后由本模块统一融合。

设计要点：
- 对外只暴露 ``search_knowledge``，返回按相关性降序的条目列表，条目字段
  保持 ``id/title/kind/content/keywords`` 不变，调用方无需感知检索实现。
- 检索分为「分词 → 打分 → 归一化 → 排序」四步。分词只做中文单字 + 英文
  词元切分，不引入额外依赖；打分使用标准 BM25，并对无界分数做归一化，
  使不同查询的分数可比较，也为后续与向量分数融合留出统一的 [0, 1] 量纲。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from backend.database import SessionLocal
from backend.models import KnowledgeDoc

# BM25 参数：k1 控制词频饱和速度，b 控制文档长度归一化强度
_BM25_K1 = 1.5
_BM25_B = 0.75

# 中文按单字切分、英文/数字按连续词元切分
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")

# 中文单字在正文中的区分度很低（「天」「日」「的」等几乎每篇都有），
# 直接参与打分会让任意一句话都命中。约定：单字只用于匹配人工标注的
# 标题与关键词，正文匹配要求至少两个字符的连续词元。
def _is_single_cjk(token: str) -> bool:
    return len(token) == 1 and "\u4e00" <= token <= "\u9fff"


@dataclass
class SearchResult:
    """一条检索结果。

    score 为归一化到 [0, 1] 的相关性分数，match_type 标注命中来源，
    便于后续混合检索时区分「关键词命中」与「向量命中」。
    """

    id: str
    title: str
    kind: str
    content: str
    keywords: list[str] = field(default_factory=list)
    score: float = 0.0
    match_type: str = "keyword"

    def to_dict(self) -> dict:
        """转换为对外字典，保持既有字段契约。"""
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "content": self.content,
            "keywords": self.keywords,
            "score": round(self.score, 4),
            "match_type": self.match_type,
        }


def tokenize(text: str) -> list[str]:
    """切分为检索词元：英文按词、中文按单字。"""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _all_docs() -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeDoc).all()
        return [r.to_dict() for r in rows]
    finally:
        db.close()


def _doc_tokens(item: dict) -> list[str]:
    """文档检索用的词元集合。

    标题与关键词是人工标注的强信号，重复计入以提升权重；
    正文是主要证据来源。正文只保留双字及以上词元，避免单字泛词
    导致任意查询都命中。
    """
    title_tokens = tokenize(item.get("title", ""))
    keyword_tokens = [tokenize(k) for k in item.get("keywords", [])]
    body_tokens = [t for t in tokenize(item.get("content", "")) if not _is_single_cjk(t)]
    tokens = list(body_tokens)
    tokens.extend(title_tokens * 2)
    for kt in keyword_tokens:
        tokens.extend(kt * 2)
    return tokens


def _bm25_scores(query_tokens: list[str], docs: list[dict]) -> list[float]:
    """对每个文档计算 BM25 分数。"""
    if not docs or not query_tokens:
        return [0.0] * len(docs)

    doc_tokens = [_doc_tokens(d) for d in docs]
    doc_lens = [len(t) for t in doc_tokens]
    avg_len = sum(doc_lens) / len(doc_lens) if doc_lens else 0.0
    if avg_len <= 0:
        return [0.0] * len(docs)

    n_docs = len(docs)
    # 文档频次：出现在多少个文档中
    df: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1

    scores: list[float] = []
    unique_query = set(query_tokens)
    for idx, tokens in enumerate(doc_tokens):
        tf: dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        score = 0.0
        for token in unique_query:
            freq = tf.get(token, 0)
            if freq == 0:
                continue
            # 平滑 IDF，避免高频词权重为负
            idf = math.log(1 + (n_docs - df[token] + 0.5) / (df[token] + 0.5))
            denom = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_lens[idx] / avg_len)
            score += idf * (freq * (_BM25_K1 + 1)) / denom
        scores.append(score)
    return scores


def _normalize(scores: list[float]) -> list[float]:
    """把无界的 BM25 分数缩放到 [0, 1]。

    以列表内最大值为基准缩放；最大值本身为 0 时整体视为无命中。
    这样不同查询的分数可直接比较，也为向量分数融合提供统一量纲。
    """
    if not scores:
        return []
    max_score = max(scores)
    if max_score <= 0:
        return [0.0] * len(scores)
    return [s / max_score for s in scores]


def search_knowledge(query: str, top_k: int = 3) -> list[dict]:
    """按相关性检索知识条目，返回降序的条目字典列表。

    只返回分数大于 0 的条目（即至少命中一个查询词元）。
    """
    query_tokens = tokenize(query)
    docs = _all_docs()
    if not docs:
        return []

    raw_scores = _bm25_scores(query_tokens, docs)
    norm_scores = _normalize(raw_scores)

    results: list[SearchResult] = []
    for item, score in zip(docs, norm_scores):
        if score <= 0:
            continue
        results.append(
            SearchResult(
                id=item["id"],
                title=item["title"],
                kind=item["kind"],
                content=item["content"],
                keywords=item.get("keywords", []),
                score=score,
                match_type="keyword",
            )
        )

    # 分数降序；同分时保持原始顺序，保证结果稳定可复现
    results.sort(key=lambda r: r.score, reverse=True)
    return [r.to_dict() for r in results[:top_k]]


def get_knowledge(knowledge_id: str) -> dict | None:
    db = SessionLocal()
    try:
        doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.id == knowledge_id).first()
        return doc.to_dict() if doc else None
    finally:
        db.close()
