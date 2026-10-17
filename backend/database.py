from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# SQLite 文件库：零外部依赖，进程重启数据不丢
_DB_FILE = Path(__file__).resolve().parent / "app.db"

_engine = create_engine(
    f"sqlite:///{_DB_FILE}",
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """建表（幂等）。"""
    from backend import models  # noqa: F401  确保模型被注册

    Base.metadata.create_all(bind=_engine)


def get_db():
    """FastAPI 依赖：提供请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
