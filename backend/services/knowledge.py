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
from backend.services.retrieval_config import RetrievalConfig, load_retrieval_config

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


# ---------------------------------------------------------------------------
# 向量召回
# ---------------------------------------------------------------------------

# 候选预取深度由配置决定（见 retrieval_config）；这里保留函数作为统一入口
def _prefetch_depth(top_k: int, config: RetrievalConfig | None = None) -> int:
    """计算召回应取回的候选条数。"""
    cfg = config or load_retrieval_config()
    return cfg.prefetch_depth(top_k)


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；任一向量为零向量时返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _doc_text(item: dict) -> str:
    """拼出用于向量化的文档文本：标题 + 关键词 + 正文。"""
    keywords = "、".join(item.get("keywords", []))
    return f"{item.get('title', '')}\n{keywords}\n{item.get('content', '')}"


def search_knowledge_by_vector(
    query: str,
    *,
    top_k: int = 3,
    embedding_provider=None,
) -> list[dict]:
    """按向量相似度检索知识条目。

    embedding 不可用（未配置密钥、网络失败、返回异常）时返回空列表，
    由调用方降级到关键词检索，避免整个检索链路因模型不可用而失败。
    """
    if embedding_provider is None:
        return []

    docs = _all_docs()
    if not docs:
        return []

    try:
        query_vector = embedding_provider.embed(query)
        doc_vectors = embedding_provider.embed_many([_doc_text(d) for d in docs])
    except Exception:  # noqa: BLE001 - 模型不可用时统一降级
        return []

    if not query_vector or len(doc_vectors) != len(docs):
        return []

    scored: list[tuple[dict, float]] = []
    for item, vec in zip(docs, doc_vectors):
        sim = _cosine(query_vector, vec)
        if sim > 0:
            scored.append((item, sim))

    scored.sort(key=lambda pair: pair[1], reverse=True)

    results: list[SearchResult] = []
    for item, sim in scored[:top_k]:
        results.append(
            SearchResult(
                id=item["id"],
                title=item["title"],
                kind=item["kind"],
                content=item["content"],
                keywords=item.get("keywords", []),
                score=max(0.0, min(1.0, sim)),
                match_type="vector",
            )
        )
    return [r.to_dict() for r in results]


# ---------------------------------------------------------------------------
# 多路融合（RRF）
# ---------------------------------------------------------------------------

# RRF 默认参数来自配置；函数签名保留可覆盖入口，便于测试与单次调参
def fuse_rrf(
    *,
    vector_results: list[dict],
    keyword_results: list[dict],
    top_k: int = 3,
    k: int | None = None,
    vector_weight: float | None = None,
    keyword_weight: float | None = None,
    config: RetrievalConfig | None = None,
) -> list[dict]:
    """用 RRF（倒数排名融合）合并两路召回结果。

    融合只依赖各自列表中的**名次**，不直接相加原始分数——向量相似度与
    BM25 分数量级不同，直接相加会让其中一路压倒另一路。

    融合分再按理论最大值 ``(vw+kw)/(k+1)`` 归一化到 [0, 1]，
    使混合结果与单路结果的分数仍可比较。
    """
    if not vector_results and not keyword_results:
        return []

    cfg = config or load_retrieval_config()
    k = cfg.rrf_k if k is None else k
    vector_weight = cfg.vector_weight if vector_weight is None else vector_weight
    keyword_weight = cfg.keyword_weight if keyword_weight is None else keyword_weight

    # 记录每个条目在两路中的 1-based 名次（同一路内重复取最优名次）
    vector_rank: dict[str, int] = {}
    keyword_rank: dict[str, int] = {}
    # 保留条目内容，优先用向量路（字段更完整），缺失时用关键词路
    items: dict[str, dict] = {}

    for rank, item in enumerate(vector_results, start=1):
        doc_id = item["id"]
        vector_rank.setdefault(doc_id, rank)
        items.setdefault(doc_id, item)
    for rank, item in enumerate(keyword_results, start=1):
        doc_id = item["id"]
        keyword_rank.setdefault(doc_id, rank)
        items.setdefault(doc_id, item)

    max_rrf = (vector_weight + keyword_weight) / (k + 1)

    fused: list[dict] = []
    for doc_id, item in items.items():
        score = 0.0
        sources: list[str] = []
        if doc_id in vector_rank:
            score += vector_weight / (k + vector_rank[doc_id])
            sources.append("vector")
        if doc_id in keyword_rank:
            score += keyword_weight / (k + keyword_rank[doc_id])
            sources.append("keyword")
        normalized = score / max_rrf if max_rrf > 0 else 0.0

        matched = set(sources)
        if matched == {"vector", "keyword"}:
            match_type = "hybrid"
        elif sources:
            match_type = sources[0]
        else:
            match_type = "keyword"

        fused.append(
            {
                **item,
                "score": round(min(1.0, max(0.0, normalized)), 4),
                "match_type": match_type,
            }
        )

    # 融合分降序；同分时按 id 稳定排序，保证结果可复现
    fused.sort(key=lambda r: (-r["score"], r["id"]))
    return fused[:top_k]


def search_knowledge_hybrid(
    query: str,
    *,
    top_k: int = 3,
    embedding_provider=None,
) -> list[dict]:
    """混合检索入口：关键词与向量两路召回后做 RRF 融合。

    向量路不可用（未配置模型或调用失败）时自动退化为纯关键词检索，
    保证检索始终有结果、不会因为模型不可用而整体失败。
    """
    depth = _prefetch_depth(top_k)

    keyword_results = search_knowledge(query, top_k=depth)
    vector_results = search_knowledge_by_vector(
        query, top_k=depth, embedding_provider=embedding_provider
    )

    if not vector_results:
        # 单路降级：直接返回关键词结果
        return keyword_results[:top_k]

    return fuse_rrf(
        vector_results=vector_results,
        keyword_results=keyword_results,
        top_k=top_k,
    )


# ---------------------------------------------------------------------------
# 相关性重排
# ---------------------------------------------------------------------------

# 送进重排模型的候选上限：每个候选都是一次计费调用，
# 超量候选只会增加成本与延迟，收益很低。
_MAX_RERANK_CANDIDATES = 200

# 阈值降级参数：当所有候选都不过阈值时，按倍数放宽一次再试。
# 降级只发生一次，且不会降到 degrade_floor 以下——过低的阈值等于没有过滤。
_DEGRADE_FLOOR = 0.3
_DEGRADE_FACTOR = 0.7

# 保底分：阈值全不过时，最佳候选至少要达到这个分数才保留。
# 低于它说明这段内容和问题基本无关，返回空比硬塞一条更诚实。
_FALLBACK_MIN_SCORE = 0.15

# 复合分权重：重排分反映「这段内容是否回答了问题」，召回分反映
# 「这段内容有多容易被找到」，来源权重用于小幅偏好权威来源。
_COMPOSITE_RERANK_WEIGHT = 0.6
_COMPOSITE_RECALL_WEIGHT = 0.3
_COMPOSITE_SOURCE_WEIGHT = 0.1

# 来源权重：规则类文档优先级略高，FAQ 次之，普通手册为基准。
_SOURCE_WEIGHTS = {
    "rule": 1.0,
    "faq": 0.8,
    "manual": 0.6,
}


def build_passage(item: dict) -> str:
    """把检索条目拼成送进重排模型的文本片段。

    顺序为标题 → 关键词 → 正文：标题与关键词是判断主题的最强信号，
    放在最前面可以让模型在长正文被截断时仍抓到重点。
    """
    title = item.get("title", "") or ""
    keywords = "、".join(item.get("keywords", []) or [])
    content = item.get("content", "") or ""
    parts = [p for p in (title, keywords, content) if p]
    return "\n".join(parts)


def composite_score(item: dict, rerank_score: float) -> float:
    """把重排分、召回分与来源权重混合成一个总分。"""
    recall = float(item.get("recall_score", item.get("score", 0.0)) or 0.0)
    source_weight = _SOURCE_WEIGHTS.get(item.get("kind", ""), 0.5)
    blended = (
        _COMPOSITE_RERANK_WEIGHT * rerank_score
        + _COMPOSITE_RECALL_WEIGHT * recall
        + _COMPOSITE_SOURCE_WEIGHT * source_weight
    )
    return min(1.0, max(0.0, blended))


def _apply_threshold(ordered: list[dict], threshold: float) -> tuple[list[dict], dict]:
    """按阈值筛选候选，必要时放宽阈值或保底保留最优一条。

    阈值比较的是**重排分**而非复合分：阈值回答的是「这段内容与问题是否
    相关」，这是重排模型的判断；复合分还混入了召回分与来源权重，
    用它做门槛会让召回容易的文档单靠召回分就通过。

    返回 (保留结果, 诊断信息)。诊断信息记录本次实际生效的阈值与结果，
    便于排查「为什么这次只召回了一条」。
    """
    diagnostics = {
        "threshold": threshold,
        "effective_threshold": threshold,
        "degraded": False,
        "fallback": False,
    }
    if threshold <= 0 or not ordered:
        return ordered, diagnostics

    def relevance(item: dict) -> float:
        return float(item.get("rerank_score", item.get("score", 0.0)))

    passing = [r for r in ordered if relevance(r) >= threshold]
    if passing:
        return passing, diagnostics

    # 第一次降级：放宽阈值后重试
    if threshold > _DEGRADE_FLOOR:
        relaxed = max(threshold * _DEGRADE_FACTOR, _DEGRADE_FLOOR)
        passing = [r for r in ordered if relevance(r) >= relaxed]
        diagnostics["effective_threshold"] = relaxed
        diagnostics["degraded"] = True
        if passing:
            return passing, diagnostics

    # 仍无结果：最佳候选的相关性达到保底分才保留，否则宁可为空
    best = ordered[0]
    if relevance(best) >= _FALLBACK_MIN_SCORE:
        diagnostics["fallback"] = True
        return [best], diagnostics
    return [], diagnostics


def rerank_results(
    query: str,
    results: list[dict],
    *,
    rerank_provider=None,
    top_k: int | None = None,
    threshold: float = 0.0,
    with_diagnostics: bool = False,
) -> list[dict] | tuple[list[dict], dict]:
    """用重排模型按与查询的相关性重新排序候选。

    重排模型只判断「这段内容与这个问题是否相关」，不负责召回，
    因此它只能调整已有候选的顺序与取舍，不会引入新条目。

    - 模型不可用或调用失败：原样返回输入顺序，不阻断检索链路。
    - ``threshold`` 用于过滤低相关候选；全部不过阈值时先放宽一次，
      仍不过则保留达到保底分的最优一条，连保底分都达不到就返回空。
    - ``with_diagnostics`` 为真时同时返回诊断信息，用于观察阈值是否降级。
    """
    if not results:
        out: list[dict] = []
        return (out, {"threshold": threshold, "effective_threshold": threshold}) if with_diagnostics else out
    if rerank_provider is None:
        sliced = results[:top_k] if top_k else results
        diag = {"threshold": threshold, "effective_threshold": threshold, "skipped": "no_provider"}
        return (sliced, diag) if with_diagnostics else sliced

    # 控制送审候选数量，避免无意义的调用成本
    candidates = results[:_MAX_RERANK_CANDIDATES]
    passages = [build_passage(r) for r in candidates]

    # 无有效文本的候选无法打分，保持原位不动
    scorable = [(i, p) for i, p in enumerate(passages) if p.strip()]
    if not scorable:
        sliced = results[:top_k] if top_k else results
        diag = {"threshold": threshold, "effective_threshold": threshold, "skipped": "no_passage"}
        return (sliced, diag) if with_diagnostics else sliced

    try:
        ranked = rerank_provider.rerank(
            query, [p for _, p in scorable], top_n=len(scorable)
        )
    except Exception:  # noqa: BLE001 - 重排失败时降级为原顺序
        sliced = results[:top_k] if top_k else results
        diag = {"threshold": threshold, "effective_threshold": threshold, "skipped": "model_error"}
        return (sliced, diag) if with_diagnostics else sliced

    if not ranked:
        sliced = results[:top_k] if top_k else results
        diag = {"threshold": threshold, "effective_threshold": threshold, "skipped": "empty"}
        return (sliced, diag) if with_diagnostics else sliced

    ordered: list[dict] = []
    for hit in ranked:
        # 重排返回的下标指向 scorable，需要映射回 candidates 的位置
        if not (0 <= hit.index < len(scorable)):
            continue
        original_index = scorable[hit.index][0]
        item = dict(candidates[original_index])
        model_score = min(1.0, max(0.0, float(hit.relevance_score)))
        item["rerank_score"] = round(model_score, 4)
        # 保留召回分，便于对比重排前后的变化
        item.setdefault("recall_score", item.get("score", 0.0))
        item["score"] = round(composite_score(item, model_score), 4)
        item["match_type"] = "rerank"
        ordered.append(item)

    if not ordered:
        sliced = results[:top_k] if top_k else results
        diag = {"threshold": threshold, "effective_threshold": threshold, "skipped": "no_valid_index"}
        return (sliced, diag) if with_diagnostics else sliced

    # 重排模型通常已按分数降序返回，这里再排一次以防供应商未排序
    ordered.sort(key=lambda r: -r["score"])

    ordered, diag = _apply_threshold(ordered, threshold)
    diag["candidates"] = len(scorable)
    diag["returned"] = len(ordered[:top_k]) if top_k else len(ordered)

    final = ordered[:top_k] if top_k else ordered
    return (final, diag) if with_diagnostics else final


# ---------------------------------------------------------------------------
# 去冗余（MMR）
# ---------------------------------------------------------------------------

# MMR 的 λ：越大越看重相关性，越小越看重多样性。
# 0.7 表示「相关性仍是主导，但明显重复的内容会被挤到后面」。
# MMR 的 λ 由配置提供，这里保留常量名作为文档提示
_MMR_LAMBDA_DEFAULT = 0.7


def _jaccard(a: set[str], b: set[str]) -> float:
    """两个词元集合的 Jaccard 相似度；任一为空时视为不相似。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _item_token_set(item: dict) -> set[str]:
    """取条目用于去重比对的词元集合。

    这里保留单字：去重看的是「两段文字是否在讲同一件事」，
    单字的重叠同样是有效信号。检索打分那侧过滤单字是为了压噪声，
    两个用途目标不同，不能共用同一套筛子。
    """
    text = build_passage(item)
    return set(tokenize(text))


def select_mmr(
    results: list[dict],
    top_k: int,
    *,
    lambda_: float | None = None,
) -> list[dict]:
    """用最大边际相关（MMR）从候选中选出既相关又互不重复的若干条。

    贪心选择：每轮挑出 ``λ×相关性 − (1−λ)×已选条目中的最大相似度``
    最高的候选。这样当多篇文档讲的是同一件事时，只有最相关的一篇
    会被保留，剩余位置让给其它角度的内容。

    输入需已按相关性降序，函数内部取 ``score`` 作为相关性。
    """
    if top_k <= 0 or not results:
        return []

    if lambda_ is None:
        lambda_ = load_retrieval_config().mmr_lambda

    candidates = list(results)
    if len(candidates) <= top_k:
        return candidates

    tokens = [_item_token_set(c) for c in candidates]
    selected: list[int] = []
    remaining = list(range(len(candidates)))

    while remaining and len(selected) < top_k:
        best_pos = 0
        best_score = float("-inf")
        for pos, idx in enumerate(remaining):
            relevance = float(candidates[idx].get("score", 0.0))
            # 首条没有冗余项，直接按相关性取
            if not selected:
                mmr = relevance
            else:
                redundancy = max(_jaccard(tokens[idx], tokens[s]) for s in selected)
                mmr = lambda_ * relevance - (1.0 - lambda_) * redundancy
            if mmr > best_score:
                best_score = mmr
                best_pos = pos
        selected.append(remaining.pop(best_pos))

    return [candidates[i] for i in selected]


def rerank_with_mmr(
    query: str,
    results: list[dict],
    *,
    rerank_provider=None,
    top_k: int = 3,
    threshold: float = 0.0,
    lambda_: float | None = None,
) -> list[dict]:
    """重排后再做 MMR 去冗余，返回最终用于回答的若干条。

    这是检索链路的收口函数：召回 → 融合 → 重排 → 阈值 → 去冗余。
    """
    candidate_count = max(top_k * 3, top_k)
    reranked = rerank_results(
        query,
        results,
        rerank_provider=rerank_provider,
        top_k=candidate_count,
        threshold=threshold,
    )
    return select_mmr(reranked, top_k, lambda_=lambda_)


# ---------------------------------------------------------------------------
# 检索诊断
# ---------------------------------------------------------------------------


def diagnose(
    query: str,
    *,
    top_k: int = 3,
    embedding_provider=None,
    rerank_provider=None,
    threshold: float = 0.0,
    config: RetrievalConfig | None = None,
) -> dict:
    """完整解释一次检索：每一环取了什么、丢了多少、为什么。

    排查「为什么搜不到」时，最耗时的往往不是修，而是不知道问题出在
    哪一环：是没召回、被融合挤掉了、被阈值拦了，还是被去冗余合并了。
    这里把每一阶段的输入输出都摊开，直接看出断点在哪。

    只读操作，不改变检索行为；可以安全地挂在调试接口上。
    """
    cfg = config or load_retrieval_config()
    depth = cfg.prefetch_depth(top_k)

    keyword_hits = search_knowledge(query, top_k=depth)
    vector_hits = search_knowledge_by_vector(
        query, top_k=depth, embedding_provider=embedding_provider
    )

    stages: list[dict] = [
        {
            "stage": "keyword_recall",
            "count": len(keyword_hits),
            "ids": [h["id"] for h in keyword_hits],
        },
        {
            "stage": "vector_recall",
            "count": len(vector_hits),
            "ids": [h["id"] for h in vector_hits],
            "skipped": not vector_hits,
        },
    ]

    if vector_hits:
        fused = fuse_rrf(
            vector_results=vector_hits,
            keyword_results=keyword_hits,
            top_k=depth,
            config=cfg,
        )
        stages.append(
            {
                "stage": "rrf_fusion",
                "count": len(fused),
                "ids": [f["id"] for f in fused],
            }
        )
    else:
        fused = keyword_hits
        stages.append(
            {
                "stage": "rrf_fusion",
                "count": len(fused),
                "ids": [f["id"] for f in fused],
                "skipped": "no_vector",
                "note": "向量路不可用，直接使用关键词结果",
            }
        )

    reranked, rerank_diag = rerank_results(
        query,
        fused,
        rerank_provider=rerank_provider,
        top_k=depth,
        threshold=threshold,
        with_diagnostics=True,
    )
    stages.append(
        {
            "stage": "rerank",
            "count": len(reranked),
            "ids": [r["id"] for r in reranked],
            "detail": rerank_diag,
        }
    )

    final = select_mmr(reranked, top_k)
    stages.append(
        {
            "stage": "mmr",
            "count": len(final),
            "ids": [f["id"] for f in final],
            "dropped": len(reranked) - len(final),
        }
    )

    return {
        "query": query,
        "top_k": top_k,
        "prefetch_depth": depth,
        "config": cfg.to_dict(),
        "stages": stages,
        "final_ids": [f["id"] for f in final],
    }





