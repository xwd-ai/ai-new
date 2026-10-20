from sqlalchemy import JSON, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    user_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    tried_steps: Mapped[str] = mapped_column(String, default="")
    evidence: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="open")  # open/in_progress/resolved
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    replies: Mapped[list] = mapped_column(JSON, default=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "dedup_key": self.dedup_key,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "description": self.description,
            "tried_steps": self.tried_steps,
            "evidence": self.evidence,
            "status": self.status,
            "created_at": self.created_at,
            "replies": self.replies,
        }
