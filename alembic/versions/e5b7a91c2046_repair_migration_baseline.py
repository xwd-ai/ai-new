"""建立完整结构并接入旧库，保留已有业务数据。"""

from alembic import op
import sqlalchemy as sa


revision = "e5b7a91c2046"
down_revision = "d1178ec7c657"
branch_labels = None
depends_on = None


def _schema() -> sa.MetaData:
    # 固定本次基线，后续模型变化必须通过新 revision 演进。
    metadata = sa.MetaData()
    sa.Table(
        "conversations", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    sa.Table(
        "knowledge_docs", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=True, index=True),
    )
    sa.Table(
        "messages", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("at", sa.Float(), nullable=False),
    )
    sa.Table(
        "orders", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("product_tier", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    sa.Table(
        "products", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
    )
    sa.Table(
        "sessions", metadata,
        sa.Column("token", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    sa.Table(
        "ticket_status_logs", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.String(), nullable=False, index=True),
        sa.Column("from_status", sa.String(), nullable=False),
        sa.Column("to_status", sa.String(), nullable=False),
        sa.Column("operator", sa.String(), nullable=False),
        sa.Column("changed_at", sa.Float(), nullable=False),
    )
    sa.Table(
        "tickets", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("dedup_key", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("user_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tried_steps", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("replies", sa.JSON(), nullable=False),
        sa.UniqueConstraint("dedup_key", name="uq_tickets_dedup_key"),
    )
    sa.Table(
        "tried_steps", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("step", sa.String(), nullable=False),
        sa.Column("at", sa.Float(), nullable=False),
    )
    sa.Table(
        "users", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    return metadata


def _has_unique(inspector, table_name: str, column_name: str) -> bool:
    uniques = inspector.get_unique_constraints(table_name)
    uniques += [
        i for i in inspector.get_indexes(table_name)
        if i["unique"] and i.get("dialect_options", {}).get("sqlite_where") is None
    ]
    return any(u["column_names"] == [column_name] for u in uniques)


def _preflight(connection, metadata: sa.MetaData) -> None:
    inspector = sa.inspect(connection)
    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        actual = {c["name"]: c for c in inspector.get_columns(table.name)}
        expected = set(table.columns.keys())
        allowed_missing = {"fingerprint"} if table.name == "knowledge_docs" else set()
        if set(actual) - expected or expected - set(actual) - allowed_missing:
            raise RuntimeError(f"表 {table.name} 的列与迁移基线不兼容，请先核对结构。")
        for column in table.columns:
            if column.name not in actual:
                continue
            found = actual[column.name]
            if not isinstance(found["type"], type(column.type)) or found["nullable"] != column.nullable:
                raise RuntimeError(f"表 {table.name} 的列 {column.name} 与迁移基线不兼容。")
        if inspector.get_pk_constraint(table.name)["constrained_columns"] != list(table.primary_key.columns.keys()):
            raise RuntimeError(f"表 {table.name} 的主键与迁移基线不兼容。")
        actual_indexes = {i["name"]: i for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            found = actual_indexes.get(index.name)
            if found is not None and (
                found["column_names"] != list(index.columns.keys())
                or bool(found["unique"]) != bool(index.unique)
                or found.get("dialect_options", {}).get("sqlite_where") is not None
            ):
                raise RuntimeError(f"索引 {index.name} 的定义与迁移基线不兼容。")
        for constraint in table.constraints:
            if isinstance(constraint, sa.UniqueConstraint):
                columns = list(constraint.columns)
                duplicate = connection.execute(
                    sa.select(*columns).group_by(*columns).having(sa.func.count() > 1).limit(1)
                ).first()
                if duplicate is not None:
                    raise RuntimeError(f"表 {table.name} 存在重复唯一键；请先人工核对，迁移不会删除数据。")
                if not _has_unique(inspector, table.name, columns[0].name):
                    trigger = connection.execute(sa.text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table"
                    ), {"table": table.name}).first()
                    if trigger or inspector.get_check_constraints(table.name) or inspector.get_foreign_keys(table.name):
                        raise RuntimeError(f"表 {table.name} 含自定义触发器或约束，不支持自动重建，请人工核对。")


def upgrade() -> None:
    connection = op.get_bind()
    metadata = _schema()
    # SQLite DDL 不保证事务回滚，因此先检查所有旧表，再执行任何结构变更。
    _preflight(connection, metadata)
    metadata.create_all(connection, checkfirst=True)
    inspector = sa.inspect(connection)
    if "fingerprint" not in {c["name"] for c in inspector.get_columns("knowledge_docs")}:
        op.add_column("knowledge_docs", sa.Column("fingerprint", sa.String(), nullable=True))
    for table_name, column_name, constraint_name in (
        ("tickets", "dedup_key", "uq_tickets_dedup_key"),
        ("users", "username", "uq_users_username"),
    ):
        inspector = sa.inspect(connection)
        if not _has_unique(inspector, table_name, column_name):
            with op.batch_alter_table(table_name) as batch:
                batch.create_unique_constraint(constraint_name, [column_name])
    for table in metadata.sorted_tables:
        for index in table.indexes:
            index.create(connection, checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("基线不支持自动回退；如需恢复，请使用升级前的数据库备份。")
