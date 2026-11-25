"""为工单号增加数据库计数器。"""

from alembic import op
import sqlalchemy as sa

revision = "c6b2d96e3401"
down_revision = "f28a6c913d72"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = sa.Table(
        "ticket_number_counter",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
    )
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table(table.name):
        table.create(connection)
        return

    columns = {column["name"]: column for column in inspector.get_columns(table.name)}
    if (
        set(columns) != set(table.columns.keys())
        or any(
            not isinstance(columns[column.name]["type"], type(column.type))
            or columns[column.name]["nullable"] != column.nullable
            for column in table.columns
        )
        or inspector.get_pk_constraint(table.name)["constrained_columns"] != ["id"]
    ):
        raise RuntimeError("ticket_number_counter 表结构不兼容，请先核对。")


def downgrade() -> None:
    raise RuntimeError("工单计数器不支持自动回退，请使用升级前的数据库备份。")
