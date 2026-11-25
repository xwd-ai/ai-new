from sqlalchemy import Integer, JSON, String, Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base, DB_TIMESTAMP


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(255), nullable=False)  # manual / faq / rule
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False)
    # 正文上限 10 万字符：MySQL 的 TEXT 只有 64KB（utf8mb4 下约 1.6 万字），
    # 必须用 MEDIUMTEXT（16MB）；其他方言回退为通用 Text
    content: Mapped[str] = mapped_column(
        Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False
    )
    # 标题与正文的规范化指纹，用于识别同一资料的重复入库
    fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "keywords": self.keywords,
            "content": self.content,
        }


class KnowledgeIngestLease(Base):
    __tablename__ = "knowledge_ingest_leases"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False)


class KnowledgeIngestJournal(Base):
    __tablename__ = "knowledge_ingest_journal"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    stage: Mapped[str] = mapped_column(String(255), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False)
