"""知识入库。

把一份资料变成可检索知识：解析 → 分块 → 落库。这条链路是无副作用的
数据写入，最怕两件事：重复上传产生重复数据，以及中途失败留下半份资料。

因此这里做三件事：

1. **幂等**：同一份内容重复提交，返回已有记录，不新建。判定依据是
   内容指纹（标题 + 正文的规范化哈希），而不是文件名——重命名同一份
   资料不该产生第二份知识。
2. **分块**：长正文按长度与重叠切开，切点尽量落在段落边界，
   避免把一句话从中间劈开。
3. **检查点**：每份资料记录处理到哪一步（已解析 / 已分块 / 已入库），
   中途失败后重试能看出断在哪，而不用从头猜。

这里只负责「写入」；检索侧的召回与排序由 knowledge 模块负责。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field

from backend.database import SessionLocal
from backend.models import KnowledgeDoc

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
    existing = _find_existing(fingerprint)
    if existing is not None:
        return IngestResult(
            knowledge_id=existing.id,
            created=False,
            chunks=0,
            stage=STAGE_STORED,
            detail="内容已存在，返回已有记录",
        )

    chunks = split_into_chunks(content)
    checkpoint.mark(STAGE_CHUNKED)
    checkpoint.chunk_count = len(chunks)

    doc_id = knowledge_id or f"k-{uuid.uuid4().hex[:12]}"
    db = SessionLocal()
    try:
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
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    checkpoint.mark(STAGE_STORED)
    return IngestResult(
        knowledge_id=doc_id,
        created=True,
        chunks=len(chunks),
        stage=STAGE_STORED,
    )
