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
    """建表（幂等），并为已存在的数据库补齐新增列。"""
    from backend import models  # noqa: F401  确保模型被注册

    Base.metadata.create_all(bind=_engine)
    _apply_light_migrations()


def _apply_light_migrations() -> None:
    """为已有表补齐后来新增的列。

    ``create_all`` 只负责建新表，不会改动已存在的表结构；本地 SQLite
    在开发期反复迭代时，旧库就会缺列并导致查询直接报错。这里做一次
    幂等的补列，避免每次改模型都要手工删库重建。

    只处理「加列」这一种最安全的变更；需要改类型或删列时应当走正式的
    迁移脚本。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(_engine)
    with _engine.begin() as conn:
        if "knowledge_docs" in inspector.get_table_names():
            existing = {c["name"] for c in inspector.get_columns("knowledge_docs")}
            if "fingerprint" not in existing:
                conn.execute(
                    text("ALTER TABLE knowledge_docs ADD COLUMN fingerprint VARCHAR")
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_knowledge_docs_fingerprint "
                        "ON knowledge_docs (fingerprint)"
                    )
                )


def get_db():
    """FastAPI 依赖：提供请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
