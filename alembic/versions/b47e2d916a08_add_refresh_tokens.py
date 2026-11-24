"""补齐刷新令牌登记表，兼容应用预建结构。"""
from alembic import op
import sqlalchemy as sa

revision = "b47e2d916a08"
down_revision = "a31d9f0276bc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = sa.Table(
        "refresh_tokens", sa.MetaData(),
        sa.Column("token", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
    )
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if inspector.has_table(table.name):
        columns = {c['name']: c for c in inspector.get_columns(table.name)}
        if set(columns) != set(table.columns.keys()) or any(
            not isinstance(columns[c.name]['type'], type(c.type))
            or columns[c.name]['nullable'] != c.nullable for c in table.columns
        ) or inspector.get_pk_constraint(table.name)['constrained_columns'] != ['token']:
            raise RuntimeError('refresh_tokens 表结构不兼容，请先人工核对。')
    else:
        table.create(connection)


def downgrade() -> None:
    raise RuntimeError('刷新令牌表不支持自动回退；请使用升级前的数据库备份。')
