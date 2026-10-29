"""为知识入库增加跨进程租约。"""

from alembic import op
import sqlalchemy as sa


revision = "c94e1a7f5208"
down_revision = "e5b7a91c2046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = sa.MetaData()
    table = sa.Table(
        "knowledge_ingest_leases", metadata,
        sa.Column("fingerprint", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if inspector.has_table(table.name):
        columns = {c["name"]: c for c in inspector.get_columns(table.name)}
        if set(columns) != set(table.columns.keys()) or any(
            not isinstance(columns[c.name]["type"], type(c.type))
            or columns[c.name]["nullable"] != c.nullable for c in table.columns
        ) or inspector.get_pk_constraint(table.name)["constrained_columns"] != ["fingerprint"]:
            raise RuntimeError("knowledge_ingest_leases 表结构不兼容，请先人工核对。")
    else:
        table.create(connection)


def downgrade() -> None:
    raise RuntimeError("入库租约不支持自动回退；如需恢复，请使用升级前的数据库备份。")
