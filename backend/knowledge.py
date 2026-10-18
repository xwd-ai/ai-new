# 知识库：产品手册 / FAQ / 服务规则
# 条目持久化到 SQLite 数据库，检索用简单关键词匹配（后续可升级为向量检索 + 混合检索 + rerank）
# 预置知识条目逻辑收敛到 backend.seed 模块

from backend.database import SessionLocal
from backend.models import KnowledgeDoc


def _all_docs() -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeDoc).all()
        return [r.to_dict() for r in rows]
    finally:
        db.close()


def search_knowledge(query: str, top_k: int = 3) -> list[dict]:
    """简单关键词匹配检索：命中知识条目关键词即返回，带来源标识。"""
    q = query.lower()
    results = []
    for item in _all_docs():
        if any(k.lower() in q or q in k.lower() for k in item["keywords"]):
            results.append(item)
    # 按匹配度排序：命中关键词越多越靠前
    def score(item):
        return sum(1 for k in item["keywords"] if k.lower() in q)

    results.sort(key=score, reverse=True)
    return results[:top_k]


def get_knowledge(knowledge_id: str) -> dict | None:
    db = SessionLocal()
    try:
        doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.id == knowledge_id).first()
        return doc.to_dict() if doc else None
    finally:
        db.close()
