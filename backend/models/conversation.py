from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base, DB_TIMESTAMP


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False)
    updated_at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ConversationCreationLock(Base):
    __tablename__ = "conversation_creation_locks"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(255), nullable=False)  # user / assistant
    # 消息正文可能远超 VARCHAR(255)，MySQL 下必须显式声明为 Text
    content: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "at": self.at,
        }


class TriedStep(Base):
    __tablename__ = "tried_steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step: Mapped[str] = mapped_column(String(255), nullable=False)
    at: Mapped[float] = mapped_column(DB_TIMESTAMP, nullable=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "user_id": self.user_id, "step": self.step, "at": self.at}
