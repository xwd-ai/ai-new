"""持久化入库阶段与可恢复输入。"""
from alembic import op
import sqlalchemy as sa

revision = "a31d9f0276bc"
down_revision = "c94e1a7f5208"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = sa.Table(
        "knowledge_ingest_journal", sa.MetaData(),
        sa.Column("fingerprint", sa.String(), primary_key=True),
        sa.Column("knowledge_id", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if inspector.has_table(table.name):
        columns = {c['name']: c for c in inspector.get_columns(table.name)}
        if set(columns) != set(table.columns.keys()) or any(
            not isinstance(columns[c.name]['type'], type(c.type))
            or columns[c.name]['nullable'] != c.nullable for c in table.columns
        ) or inspector.get_pk_constraint(table.name)['constrained_columns'] != ['fingerprint']:
            raise RuntimeError('knowledge_ingest_journal 表结构不兼容，请先人工核对。')
    else:
        table.create(connection)


def downgrade() -> None:
    raise RuntimeError('入库 journal 不支持自动回退；请使用升级前的数据库备份。')
