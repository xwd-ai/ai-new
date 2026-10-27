from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # manual / faq / rule
    title: Mapped[str] = mapped_column(String, nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    # 内容指纹：用于识别重复入库的同内容资料，重命名不会产生第二份
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "keywords": self.keywords,
            "content": self.content,
        }
