import os
from pathlib import Path

from sqlalchemy import Float, String, create_engine
from sqlalchemy.dialects.mysql import DOUBLE, VARCHAR
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# 数据库连接：默认 SQLite（零外部依赖），可通过 DATABASE_URL 切换到 MySQL。
# 例：mysql+pymysql://ainew:ainew@127.0.0.1:3307/ainew?charset=utf8mb4
DB_TIMESTAMP = Float().with_variant(DOUBLE(asdecimal=False), "mysql")
DB_TOKEN = String(64).with_variant(VARCHAR(2048, charset="ascii", collation="ascii_bin"), "mysql")

_DB_FILE = Path(__file__).resolve().parent / "app.db"


def _build_database_url() -> str:
    """决定使用哪个数据库：显式 DATABASE_URL 优先，否则回退本地 SQLite。"""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    if os.getenv("DB_DRIVER") == "mysql":
        from sqlalchemy.engine import URL

        return URL.create(
            "mysql+pymysql",
            username=os.getenv("MYSQL_USER", "ainew"), password=os.getenv("MYSQL_PASSWORD", "ainew"),
            host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3307")),
            database=os.getenv("MYSQL_DATABASE", "ainew"), query={"charset": "utf8mb4"},
        ).render_as_string(hide_password=False)
    return f"sqlite:///{_DB_FILE}"


def _create_engine(url: str):
    """按方言选择连接参数。

    SQLite 是单文件库，需要关闭线程检查；服务端数据库则显式配置连接池，
    避免默认池过小让并发请求在应用层排队。
    """
    if url.startswith("sqlite"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=False,
        )
    return create_engine(
        url,
        pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
        pool_pre_ping=True,  # 取连接前探活，避免复用被服务端断开的连接
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
        echo=False,
    )


DATABASE_URL = _build_database_url()

_engine = _create_engine(DATABASE_URL)

# 对外暴露引擎：迁移脚本需要与应用共用同一个连接目标
engine = _engine

SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


def dialect_name() -> str:
    """当前数据库方言（sqlite / mysql / postgresql）。

    锁策略、补列等需要按方言分支的逻辑统一从这里取，避免各处硬编码判断。
    """
    return _engine.dialect.name


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """建表（幂等），并为已存在的 SQLite 库补齐新增列。"""
    from backend import models  # noqa: F401  确保模型被注册

    Base.metadata.create_all(bind=_engine)
    _apply_light_migrations()


def _apply_light_migrations() -> None:
    """为已有表补齐后来新增的列（仅 SQLite）。

    ``create_all`` 只负责建新表，不会改动已存在的表结构；本地 SQLite
    在开发期反复迭代时，旧库就会缺列并导致查询直接报错。这里做一次
    幂等的补列，避免每次改模型都要手工删库重建。

    服务端数据库（MySQL / PostgreSQL）不在运行期补列：一是
    ``CREATE INDEX IF NOT EXISTS`` 等语法各家并不通用，二是生产库的
    结构变更应当可追溯、可回滚，统一交给迁移脚本维护。
    """
    if dialect_name() != "sqlite":
        return

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
