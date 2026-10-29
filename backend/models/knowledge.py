from sqlalchemy import Float, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # manual / faq / rule
    title: Mapped[str] = mapped_column(String, nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    # 标题与正文的规范化指纹，用于识别同一资料的重复入库
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

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

    fingerprint: Mapped[str] = mapped_column(String, primary_key=True)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
