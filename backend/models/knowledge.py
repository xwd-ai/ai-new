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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "keywords": self.keywords,
            "content": self.content,
        }
