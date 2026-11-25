"""MySQL 运行时精度、令牌长度与跨进程会话锁。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DOUBLE, VARCHAR

revision = "a78d6ef01142"
down_revision = "c6b2d96e3401"
branch_labels = None
depends_on = None

TIMESTAMPS = {
    "conversations": ("created_at", "updated_at"),
    "messages": ("at",),
    "tried_steps": ("at",),
    "orders": ("started_at", "expires_at"),
    "tickets": ("created_at",),
    "ticket_status_logs": ("changed_at",),
    "sessions": ("expires_at",),
    "refresh_tokens": ("expires_at", "revoked_at"),
    "revoked_tokens": ("expires_at", "revoked_at"),
    "knowledge_ingest_leases": ("expires_at",),
    "knowledge_ingest_journal": ("updated_at",),
}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("conversation_creation_locks"):
        op.create_table(
            "conversation_creation_locks",
            sa.Column("user_id", sa.String(255), primary_key=True),
        )
    if conn.dialect.name != "mysql":
        return

    for table, names in TIMESTAMPS.items():
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        for name in names:
            column = columns[name]
            if not isinstance(column["type"], DOUBLE):
                op.alter_column(table, name, existing_type=column["type"],
                                existing_nullable=column["nullable"], type_=DOUBLE())
    for table in ("sessions", "refresh_tokens"):
        token = next(column for column in inspector.get_columns(table) if column["name"] == "token")
        op.alter_column(table, "token", existing_type=token["type"], existing_nullable=False,
                        type_=VARCHAR(2048, charset="ascii", collation="ascii_bin"))
    password_hash = next(column for column in inspector.get_columns("users") if column["name"] == "password_hash")
    if password_hash["type"].length < 64:
        op.alter_column(
            "users", "password_hash", existing_type=password_hash["type"],
            existing_nullable=False, type_=sa.String(64),
        )
    index_exists = conn.execute(sa.text("""
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = DATABASE() AND table_name = 'users'
          AND index_name = 'uq_users_username_lower'
    """)).first()
    if not index_exists:
        op.execute("CREATE UNIQUE INDEX uq_users_username_lower ON users ((LOWER(username)))")


def downgrade() -> None:
    raise RuntimeError("此迁移不支持自动回退，请使用升级前的数据库备份。")
