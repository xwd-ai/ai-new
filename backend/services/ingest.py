"""知识入库。

把一份资料变成可检索知识：解析 → 分块 → 落库。这条链路是无副作用的
数据写入，最怕两件事：重复上传产生重复数据，以及中途失败留下半份资料。

因此这里做三件事：

1. **幂等**：同一份内容重复提交，返回已有记录，不新建。判定依据是
   内容指纹（标题 + 正文的规范化哈希）；修改标题可能生成新的资料。
2. **分块**：长正文按长度与重叠切开，切点尽量落在段落边界，
   避免把一句话从中间劈开。
3. **检查点**：每份资料记录处理到哪一步（已解析 / 已分块 / 已入库），
   中途失败后重试能看出断在哪，而不用从头猜。

这里只负责「写入」；检索侧的召回与排序由 knowledge 模块负责。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from backend.database import SessionLocal
from backend.models import KnowledgeDoc, KnowledgeIngestJournal
from backend.schemas.knowledge import IngestRequest
from backend.services import ingest_leases

# 分块参数：单块目标长度与相邻块重叠，保证跨块语义不断裂
CHUNK_SIZE = 512
CHUNK_OVERLAP = 80

# 入库阶段名，用于检查点
STAGE_PARSED = "parsed"
STAGE_CHUNKED = "chunked"
STAGE_STORED = "stored"

# 允许的知识类型
_ALLOWED_KINDS = {"manual", "faq", "rule"}


@dataclass
class IngestResult:
    """一次入库的结果。"""

    knowledge_id: str
    # 是否为新建（False 表示命中已有记录，属于幂等返回）
    created: bool
    chunks: int
    stage: str
    # 命中的已有记录时的说明
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "knowledge_id": self.knowledge_id,
            "created": self.created,
            "chunks": self.chunks,
            "stage": self.stage,
            "detail": self.detail,
        }


@dataclass
class IngestCheckpoint:
    """入库过程的检查点，记录这份资料处理到了哪一步。"""

    stages: list[str] = field(default_factory=list)
    chunk_count: int = 0

    def mark(self, stage: str) -> None:
        if stage not in self.stages:
            self.stages.append(stage)

    def reached(self, stage: str) -> bool:
        return stage in self.stages


def content_fingerprint(title: str, content: str) -> str:
    """计算内容指纹。

    先做规范化再哈希：同一段文字因为排版差异（多余空格、空行、全角
    空格）被算成两份，会让幂等失效，而这类差异本身不影响检索效果。
    """
    normalized_title = _normalize_text(title)
    normalized_content = _normalize_text(content)
    payload = f"{normalized_title}\n{normalized_content}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_text(text: str) -> str:
    """统一空白，便于同一内容的不同排版得到同一指纹。

    中文不靠空格分词，正文里多出来的空格纯属排版噪声，直接删掉；
    英文单词之间的空格要保留，否则会改变词义。因此先把连续空白折叠，
    再去掉「两侧都是中文」的空格。
    """
    if not text:
        return ""
    # 全角空格按半角处理，避免中英文混排时被当成不同内容
    unified = (text or "").replace("\u3000", " ")
    # 连续空白折叠为单个空格（保留英文词间的空格）
    unified = re.sub(r"\s+", " ", unified).strip()
    # 去掉两侧均为中文的那个空格：中文排版不需要它
    unified = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", unified)
    # 中文与标点之间的空格同样是噪声
    unified = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[，。；：、！？（）])", "", unified)
    unified = re.sub(r"(?<=[，。；：、！？（）])\s+(?=[\u4e00-\u9fff])", "", unified)
    return unified


def split_into_chunks(
    content: str,
    *,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """把正文切成分块。

    优先在段落边界断开：块太短没有信息量，把段落切断又会让条件与结论
    分家。找不到合适边界时退回硬切，但保证不产生空块。
    """
    text = (content or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # 在尾部一段范围内找段落/句子边界，找不到就用原切点
            window = text[start:end]
            for sep in ("\n\n", "\n", "。", "；", "，", " "):
                pos = window.rfind(sep)
                if pos > size // 2:
                    end = start + pos + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        # 下一块回退 overlap，保持上下文衔接；至少前进一个字符避免死循环
        start = max(end - overlap, start + 1)
    return chunks


def _derive_keywords(title: str, content: str, extra: list[str] | None) -> list[str]:
    """整理关键词。

    调用方显式给出的关键词优先；没有时从标题里取长度合适的片段作为
    兜底，保证新入库的资料至少能被标题中的词命中。
    """
    if extra:
        cleaned = [k.strip() for k in extra if k and k.strip()]
        if cleaned:
            return cleaned
    fallback: list[str] = []
    for token in re.findall(r"[\u4e00-\u9fff]{2,6}|[a-zA-Z]{3,}", title or ""):
        if token not in fallback:
            fallback.append(token)
    return fallback[:8]


def _find_existing(fingerprint: str) -> KnowledgeDoc | None:
    """按指纹查找已入库的资料。"""
    db = SessionLocal()
    try:
        return (
            db.query(KnowledgeDoc)
            .filter(KnowledgeDoc.fingerprint == fingerprint)
            .first()
        )
    finally:
        db.close()


def ingest(
    *,
    title: str,
    content: str,
    kind: str = "manual",
    keywords: list[str] | None = None,
    knowledge_id: str | None = None,
) -> IngestResult:
    """把一份资料入库，重复提交时幂等返回。

    返回的 ``created`` 为 False 表示这份内容此前已经入库过，
    调用方据此区分「新建」与「已存在」，也可以直接跳过后继处理。
    """
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    if not content:
        raise ValueError("正文不能为空")
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"不支持的知识类型: {kind}")

    checkpoint = IngestCheckpoint()
    checkpoint.mark(STAGE_PARSED)

    fingerprint = content_fingerprint(title, content)
    with ingest_leases.hold(fingerprint, SessionLocal) as lease:
        # 必须在获取租约之后重查，避免先前的执行者刚完成入库。
        existing = _find_existing(fingerprint)
        if existing is not None:
            with SessionLocal() as db:
                lease.refresh(db)
                journal = db.get(KnowledgeIngestJournal, fingerprint)
                if journal is not None:
                    journal.stage, journal.knowledge_id = STAGE_STORED, existing.id
                    journal.payload, journal.updated_at = {}, time.time()
                db.commit()
            record_checkpoint(fingerprint, STAGE_STORED, existing.id)
            return IngestResult(
                knowledge_id=existing.id,
                created=False,
                chunks=0,
                stage=STAGE_STORED,
                detail="内容已存在，返回已有记录",
            )

        record_checkpoint(fingerprint, STAGE_PARSED, "")

        payload, doc_id, chunk_count = _journal_start(
            lease, dict(title=title, content=content, kind=kind, keywords=keywords or []),
            knowledge_id or f"k-{uuid.uuid4().hex[:12]}",
        )
        # 同指纹重试使用首次持久化的输入，避免恢复时悄悄改变类型或关键词。
        title, content, kind, keywords = (payload[k] for k in ('title', 'content', 'kind', 'keywords'))
        if chunk_count is None:
            chunk_count = len(split_into_chunks(content))
            _journal_chunked(lease, chunk_count)
        checkpoint.mark(STAGE_CHUNKED)
        checkpoint.chunk_count = chunk_count

        created = True
        db = SessionLocal()
        try:
            # 续租检查与知识落库同一事务；失去所有权的旧执行者不能提交。
            lease.refresh(db)
            existing = db.query(KnowledgeDoc).filter(KnowledgeDoc.fingerprint == fingerprint).first()
            if existing is not None:
                doc_id = existing.id
                created = False
            else:
                db.add(
                    KnowledgeDoc(
                        id=doc_id,
                        kind=kind,
                        title=title,
                        keywords=_derive_keywords(title, content, keywords),
                        content=content,
                        fingerprint=fingerprint,
                    )
                )
            journal = db.get(KnowledgeIngestJournal, fingerprint)
            journal.stage = STAGE_STORED
            journal.knowledge_id = doc_id
            journal.payload = {}  # 完成后无需重复保留整篇输入。
            journal.updated_at = time.time()
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        checkpoint.mark(STAGE_STORED)
        record_checkpoint(fingerprint, STAGE_STORED, doc_id)
        return IngestResult(
            knowledge_id=doc_id,
            created=created,
            chunks=chunk_count if created else 0,
            stage=STAGE_STORED,
            detail="" if created else "内容已存在，返回已有记录",
        )


def _journal_start(lease, payload: dict, doc_id: str) -> tuple[dict, str, int | None]:
    with SessionLocal() as db:
        lease.refresh(db)
        row = db.get(KnowledgeIngestJournal, lease.fingerprint)
        if row is None:
            row = KnowledgeIngestJournal(
                fingerprint=lease.fingerprint, knowledge_id=doc_id, payload=payload,
                stage=STAGE_PARSED, chunk_count=0, updated_at=time.time(),
            )
            db.add(row)
        elif row.stage == STAGE_STORED:
            existing = db.get(KnowledgeDoc, row.knowledge_id)
            if existing is None or existing.fingerprint != lease.fingerprint:
                raise ValueError('完成记录与知识资料不一致，请人工核对')
            result = (payload, row.knowledge_id, row.chunk_count)
            db.commit()
            return result
        elif db.get(KnowledgeDoc, row.knowledge_id) is not None:
            # 先前指定的编号已被其他资料占用，允许普通重提使用新的编号。
            row.knowledge_id = doc_id
        if row.stage not in (STAGE_PARSED, STAGE_CHUNKED):
            raise ValueError('入库 journal 状态不兼容，请人工核对')
        validated = IngestRequest.model_validate(row.payload).model_dump()
        if content_fingerprint(validated['title'], validated['content']) != lease.fingerprint:
            raise ValueError('入库 journal 指纹不匹配，请人工核对')
        result = (validated, row.knowledge_id, row.chunk_count if row.stage == STAGE_CHUNKED else None)
        if result[2] is not None and result[2] < 1:
            raise ValueError('入库 journal 分块计数无效，请人工核对')
        db.commit()
        return result


def _journal_chunked(lease, chunk_count: int) -> None:
    with SessionLocal() as db:
        lease.refresh(db)
        row = db.get(KnowledgeIngestJournal, lease.fingerprint)
        row.stage, row.chunk_count, row.updated_at = STAGE_CHUNKED, chunk_count, time.time()
        db.commit()


def pending_ingests() -> list[dict]:
    with SessionLocal() as db:
        rows = db.query(KnowledgeIngestJournal).filter(
            KnowledgeIngestJournal.stage != STAGE_STORED,
        ).order_by(KnowledgeIngestJournal.updated_at, KnowledgeIngestJournal.fingerprint).limit(100).all()
        return [dict(fingerprint=r.fingerprint, knowledge_id=r.knowledge_id,
                     stage=r.stage, chunk_count=r.chunk_count, updated_at=r.updated_at) for r in rows]


def recover_ingest(fingerprint: str) -> IngestResult:
    with SessionLocal() as db:
        row = db.get(KnowledgeIngestJournal, fingerprint)
        if row is None:
            raise LookupError('未找到入库 journal')
        if row.stage == STAGE_STORED:
            doc = db.get(KnowledgeDoc, row.knowledge_id)
            if doc is None or doc.fingerprint != fingerprint:
                raise ValueError('完成记录与知识资料不一致，请人工核对')
            return IngestResult(doc.id, False, 0, STAGE_STORED, '内容已存在，返回已有记录')
        occupied = db.get(KnowledgeDoc, row.knowledge_id)
        if occupied is not None and occupied.fingerprint != fingerprint:
            raise ValueError('预留资料编号已被其他资料占用，请人工核对或重新提交')
        payload = IngestRequest.model_validate(row.payload).model_dump()
        if content_fingerprint(payload['title'], payload['content']) != fingerprint:
            raise ValueError('入库 journal 指纹不匹配，请人工核对')
        doc_id = row.knowledge_id
    return ingest(**payload, knowledge_id=doc_id)


# ---------------------------------------------------------------------------
# 旧 JSON 检查点：仅旁路诊断，恢复以 SQLite journal 为准。
# ---------------------------------------------------------------------------

# 已处理指纹的记录：持久化到磁盘，进程重启后仍能判断「这份内容做过没有」。
# 只记录指纹与阶段，不记录内容——内容本身在知识表里，重复存一份纯属浪费。
_CHECKPOINT_TTL_SECONDS = 7 * 24 * 3600


def _checkpoint_path() -> Path:
    """检查点文件位置：与数据库同目录，随项目一起被忽略提交。"""
    return Path(__file__).resolve().parent.parent / "ingest_checkpoints.json"


def _load_checkpoints() -> dict[str, dict]:
    """读取检查点；文件损坏或不存在时返回空表。"""
    path = _checkpoint_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 检查点损坏不应阻断入库：当作没有记录，重新处理即可
        return {}
    if not isinstance(raw, dict):
        return {}
    records = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            if not math.isfinite(float(value.get("at", 0))):
                continue
        except (TypeError, ValueError):
            continue
        records[key] = value
    return records


def _save_checkpoints(data: dict[str, dict]) -> None:
    """写回检查点；写入失败不影响主流程。

    检查点只是加速与诊断用的旁路信息：丢掉它最多导致重做一步，
    不该让一次成功的入库因为写不了这个文件而失败。
    """
    path = _checkpoint_path()
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        return


def record_checkpoint(fingerprint: str, stage: str, knowledge_id: str) -> None:
    """记录某份内容的处理阶段。"""
    data = _load_checkpoints()
    now = time.time()
    # 顺带清理过期记录，避免文件无限增长
    data = {
        k: v
        for k, v in data.items()
        if now - float(v.get("at", 0)) < _CHECKPOINT_TTL_SECONDS
    }
    data[fingerprint] = {
        "stage": stage,
        "knowledge_id": knowledge_id,
        "at": now,
    }
    _save_checkpoints(data)


def previous_stage(fingerprint: str) -> dict | None:
    """查询这份内容上次处理到哪一步；没有记录时返回 None。

    用于排查「为什么重试很快返回」：若上次已入库，本次命中幂等；
    若上次停在 parsed，说明当时是分块或写库阶段失败的。
    """
    data = _load_checkpoints()
    record = data.get(fingerprint)
    if not record:
        return None
    if time.time() - float(record.get("at", 0)) >= _CHECKPOINT_TTL_SECONDS:
        return None
    return record
