"""补齐订单、工单查询及令牌过期清理索引。"""
from alembic import op
import sqlalchemy as sa

revision = "f28a6c913d72"
down_revision = "d9236e507b14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 冻结索引定义，避免未来模型变动改变本次迁移的行为。
    columns = {
        'orders': ['user_id', 'status', 'expires_at'],
        'tickets': ['user_id', 'status', 'created_at'],
        'sessions': ['expires_at'],
        'revoked_tokens': ['expires_at'],
        'refresh_tokens': ['expires_at'],
    }
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    # 使用 SQLite 的 ASCII 大小写规则匹配名称，保留真实名称供反射结果查找。
    owners = {key: (name, table) for key, name, table in connection.exec_driver_sql(
        "SELECT lower(name), name, lower(tbl_name) FROM sqlite_master WHERE type='index'"
    ).all()}
    missing = []
    # SQLite DDL 不保证回滚，先检查全部目标再创建缺失索引。
    for table, names in columns.items():
        existing = {index['name']: index for index in inspector.get_indexes(table)}
        for column in names:
            name = f'ix_{table}_{column}'
            owner = owners.get(name)
            if owner is not None and owner[1] != table:
                raise RuntimeError(f'索引 {name} 所属表不兼容，请先人工核对。')
            index = existing.get(owner[0]) if owner is not None else None
            if index is None:
                # SQLAlchemy 会跳过表达式索引，目录中已有同名项时不可当作缺失。
                if owner is not None:
                    raise RuntimeError(f'索引 {name} 定义不兼容，请先人工核对。')
                missing.append((name, table, column))
            elif (index['column_names'] != [column] or index['unique']
                  or index.get('dialect_options', {}).get('sqlite_where') is not None):
                raise RuntimeError(f'索引 {name} 定义不兼容，请先人工核对。')
    for name, table, column in missing:
        op.create_index(name, table, [column], unique=False)


def downgrade() -> None:
    raise RuntimeError('查询索引迁移不支持自动回退，请使用升级前的数据库备份。')
