"""补齐令牌吊销记录表及用户索引。"""
from alembic import op
import sqlalchemy as sa

revision = "d9236e507b14"
down_revision = "b47e2d916a08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = sa.Table(
        "revoked_tokens", sa.MetaData(),
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("token_type", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
    )
    index = sa.Index("ix_revoked_tokens_user_id", table.c.user_id)
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table(table.name):
        table.create(connection)
        return

    columns = {column['name']: column for column in inspector.get_columns(table.name)}
    if set(columns) != set(table.columns.keys()) or any(
        not isinstance(columns[column.name]['type'], type(column.type))
        or columns[column.name]['nullable'] != column.nullable for column in table.columns
    ) or inspector.get_pk_constraint(table.name)['constrained_columns'] != ['jti']:
        raise RuntimeError('revoked_tokens 表结构不兼容，请先人工核对。')

    existing = next((entry for entry in inspector.get_indexes(table.name) if entry['name'] == index.name), None)
    if existing is None:
        index.create(connection)
        return
    predicate = existing.get('dialect_options', {}).get('sqlite_where')
    if existing['column_names'] != ['user_id'] or existing['unique'] or predicate is not None:
        raise RuntimeError('revoked_tokens 用户索引不兼容，请先人工核对。')


def downgrade() -> None:
    raise RuntimeError('令牌吊销表不支持自动回退，请使用升级前的数据库备份。')
