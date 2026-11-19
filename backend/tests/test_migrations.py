"""迁移必须覆盖空库及旧库接入，所有操作只指向临时数据库。"""

from pathlib import Path
from contextlib import nullcontext

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from backend import database, models  # noqa: F401
from backend.models.user import RefreshToken, RevokedToken


ROOT = Path(__file__).resolve().parents[2]
OLD_REVISION = "d1178ec7c657"
HEAD_REVISION = "f28a6c913d72"


QUERY_INDEXES = {
    'orders': ['user_id', 'status', 'expires_at'],
    'tickets': ['user_id', 'status', 'created_at'],
    'sessions': ['expires_at'],
    'revoked_tokens': ['expires_at'],
    'refresh_tokens': ['expires_at'],
}


@pytest.mark.parametrize('precreated', [None, 'lower', 'upper'])
def test_query_indexes_upgrade_preserves_records_and_existing_indexes(migration_env, precreated):
    config, connection = migration_env
    command.upgrade(config, 'd9236e507b14')
    tables = sa.MetaData()
    tables.reflect(connection)
    for name in QUERY_INDEXES:
        table = tables.tables[name]
        record = {
            column.name: 123.0 if isinstance(column.type, sa.Float)
            else [] if isinstance(column.type, sa.JSON) else 'kept'
            for column in table.columns
        }
        connection.execute(table.insert(), record)
        if precreated:
            for column in QUERY_INDEXES[name]:
                index_name = f'ix_{name}_{column}'
                if precreated == 'upper':
                    index_name = index_name.upper()
                connection.exec_driver_sql(f'CREATE INDEX {index_name} ON {name} ({column})')
    connection.exec_driver_sql('CREATE INDEX custom_order_status ON orders (status)')
    connection.commit()
    before = {name: connection.execute(table.select()).all()
              for name, table in tables.tables.items() if name in QUERY_INDEXES}
    command.upgrade(config, 'head')
    command.upgrade(config, 'head')
    assert _version(connection) == HEAD_REVISION
    for name, records in before.items():
        assert connection.execute(tables.tables[name].select()).all() == records
        indexes = {index['name'].lower(): index for index in sa.inspect(connection).get_indexes(name)}
        for column in QUERY_INDEXES[name]:
            index = indexes[f'ix_{name}_{column}']
            if precreated == 'upper':
                assert index['name'] == f'ix_{name}_{column}'.upper()
            assert index['column_names'] == [column]
            assert not index['unique']
            assert index.get('dialect_options', {}).get('sqlite_where') is None
    assert 'custom_order_status' in {
        index['name'] for index in sa.inspect(connection).get_indexes('orders')
    }


@pytest.mark.parametrize('index_sql', [
    'CREATE INDEX ix_refresh_tokens_expires_at ON refresh_tokens (user_id)',
    'CREATE UNIQUE INDEX ix_refresh_tokens_expires_at ON refresh_tokens (expires_at)',
    'CREATE INDEX ix_refresh_tokens_expires_at ON refresh_tokens (expires_at) WHERE expires_at > 0',
    'CREATE INDEX ix_refresh_tokens_expires_at ON orders (expires_at)',
    'CREATE INDEX ix_refresh_tokens_expires_at ON refresh_tokens (expires_at + 0)',
    'CREATE INDEX IX_REFRESH_TOKENS_EXPIRES_AT ON refresh_tokens (user_id)',
])
def test_query_indexes_conflict_is_rejected_before_creating_any_index(migration_env, index_sql):
    config, connection = migration_env
    command.upgrade(config, 'd9236e507b14')
    connection.exec_driver_sql(index_sql)
    connection.commit()
    before = connection.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type='index'").all()
    warning = (pytest.warns(sa.exc.SAWarning, match='Skipped unsupported reflection')
               if '+ 0' in index_sql else nullcontext())
    with warning, pytest.raises(RuntimeError, match='索引 ix_refresh_tokens_expires_at.*不兼容'):
        command.upgrade(config, 'head')
    assert _version(connection) == 'd9236e507b14'
    assert connection.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type='index'").all() == before


def test_refresh_upgrade_preserves_precreated_records(migration_env):
    config, connection = migration_env
    command.upgrade(config, 'a31d9f0276bc')
    assert not sa.inspect(connection).has_table('refresh_tokens')
    RefreshToken.__table__.create(connection)
    connection.execute(RefreshToken.__table__.insert(), {
        'token': 'test-only', 'user_id': 'old-user', 'expires_at': 123.0, 'revoked_at': 100.0,
    })
    connection.commit()
    command.upgrade(config, 'head')
    command.upgrade(config, 'head')
    assert connection.scalar(sa.text("SELECT revoked_at FROM refresh_tokens WHERE token='test-only'")) == 100.0
    assert compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []


def test_refresh_upgrade_rejects_incompatible_precreated_table(migration_env):
    config, connection = migration_env
    command.upgrade(config, 'a31d9f0276bc')
    connection.exec_driver_sql('CREATE TABLE refresh_tokens (token INTEGER PRIMARY KEY)')
    connection.commit()
    with pytest.raises(RuntimeError, match='refresh_tokens 表结构不兼容'):
        command.upgrade(config, 'head')
    assert _version(connection) == 'a31d9f0276bc'
    assert sa.inspect(connection).get_columns('refresh_tokens')[0]['name'] == 'token'


def test_refresh_downgrade_refuses_to_drop_records(migration_env):
    config, connection = migration_env
    command.upgrade(config, 'b47e2d916a08')
    with pytest.raises(RuntimeError, match='不支持自动回退'):
        command.downgrade(config, 'a31d9f0276bc')
    assert sa.inspect(connection).has_table('refresh_tokens')


@pytest.mark.parametrize('with_index', [True, False])
def test_revoked_upgrade_preserves_precreated_records(migration_env, with_index):
    config, connection = migration_env
    command.upgrade(config, 'b47e2d916a08')
    table = RevokedToken.__table__
    if with_index:
        table.create(connection)
    else:
        connection.execute(sa.schema.CreateTable(table))
    record = dict(jti='test-only', user_id='old-user', token_type='access',
                  expires_at=123.0, revoked_at=100.0, reason='logout')
    connection.execute(table.insert(), record)
    connection.commit()
    command.upgrade(config, 'head')
    command.upgrade(config, 'head')
    assert dict(connection.execute(table.select()).mappings().one()) == record
    assert compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []


@pytest.mark.parametrize('index_sql', [
    'CREATE INDEX ix_revoked_tokens_user_id ON revoked_tokens (token_type)',
    "CREATE INDEX ix_revoked_tokens_user_id ON revoked_tokens (user_id) WHERE token_type='access'",
])
def test_revoked_upgrade_rejects_incompatible_index_without_replacing_it(migration_env, index_sql):
    config, connection = migration_env
    command.upgrade(config, 'b47e2d916a08')
    connection.execute(sa.schema.CreateTable(RevokedToken.__table__))
    connection.exec_driver_sql(index_sql)
    connection.commit()
    with pytest.raises(RuntimeError, match='用户索引不兼容'):
        command.upgrade(config, 'head')
    assert _version(connection) == 'b47e2d916a08'
    assert connection.scalar(sa.text(
        "SELECT sql FROM sqlite_master WHERE name='ix_revoked_tokens_user_id'"
    )) == index_sql


@pytest.fixture()
def migration_env(tmp_path, monkeypatch):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    # 即使连接注入失效，也不能误连实际 app.db。
    monkeypatch.setattr(database, "engine", engine)
    with engine.connect() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        yield config, connection
    engine.dispose()


def _version(connection):
    if not sa.inspect(connection).has_table("alembic_version"):
        return None
    return connection.scalar(sa.text("SELECT version_num FROM alembic_version"))


def _legacy_schema(connection, *, fingerprint=True, check=False):
    metadata = sa.MetaData()
    for table in database.Base.metadata.sorted_tables:
        if table.name in ("knowledge_ingest_leases", "knowledge_ingest_journal", "refresh_tokens", "revoked_tokens"):
            continue
        if table.name == "knowledge_docs" and not fingerprint:
            sa.Table(
                table.name, metadata,
                *(sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
                  for c in table.columns if c.name != "fingerprint"),
            )
        else:
            table.to_metadata(metadata)
    tickets = metadata.tables["tickets"]
    for constraint in list(tickets.constraints):
        if isinstance(constraint, sa.UniqueConstraint):
            tickets.constraints.remove(constraint)
    if check:
        tickets.append_constraint(sa.CheckConstraint("length(description) > 0"))
    metadata.create_all(connection)
    connection.execute(
        tickets.insert(),
        dict(id="T-kept", dedup_key="key-kept", user_id="u-kept", user_name="用户",
             description="保留工单", tried_steps="重启", evidence="手册", status="open",
             created_at=1.0, replies=[{"content": "已有回复"}]),
    )
    connection.execute(
        metadata.tables["knowledge_docs"].insert(),
        dict(id="k-kept", kind="manual", title="保留资料", keywords=["导出"], content="正文"),
    )
    connection.commit()


def test_empty_database_upgrade_matches_application_schema(migration_env):
    config, connection = migration_env
    command.upgrade(config, "head")
    assert _version(connection) == HEAD_REVISION
    assert set(sa.inspect(connection).get_table_names()) == {
        *database.Base.metadata.tables, "alembic_version",
    }
    assert compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []


def test_journal_upgrade_from_previous_head_preserves_knowledge(migration_env):
    config, connection = migration_env
    command.upgrade(config, 'c94e1a7f5208')
    assert not sa.inspect(connection).has_table('knowledge_ingest_journal')
    connection.execute(sa.text("INSERT INTO knowledge_docs (id,kind,title,keywords,content) VALUES ('kept','faq','旧资料','[]','正文')"))
    connection.commit()
    command.upgrade(config, 'head')
    connection.commit()
    assert connection.scalar(sa.text("SELECT content FROM knowledge_docs WHERE id='kept'")) == '正文'
    assert compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []


def test_journal_upgrade_rejects_incompatible_precreated_table(migration_env):
    config, connection = migration_env
    command.upgrade(config, 'c94e1a7f5208')
    connection.exec_driver_sql('CREATE TABLE knowledge_ingest_journal (fingerprint INTEGER PRIMARY KEY)')
    connection.commit()
    with pytest.raises(RuntimeError, match='表结构不兼容'):
        command.upgrade(config, 'head')
    assert _version(connection) == 'c94e1a7f5208'


@pytest.mark.parametrize("stamped", [False, True])
def test_legacy_database_upgrade_preserves_data_and_repairs_uniqueness(migration_env, stamped):
    config, connection = migration_env
    _legacy_schema(connection)
    before = connection.execute(sa.text("SELECT * FROM tickets")).fetchall()
    if stamped:
        command.stamp(config, OLD_REVISION)
    command.upgrade(config, "head")
    assert _version(connection) == HEAD_REVISION
    assert connection.execute(sa.text("SELECT * FROM tickets")).fetchall() == before
    assert compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []
    command.upgrade(config, "head")
    assert connection.execute(sa.text("SELECT * FROM tickets")).fetchall() == before
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(sa.text(
            "INSERT INTO tickets SELECT 'T-duplicate', dedup_key, user_id, user_name, "
            "description, tried_steps, evidence, status, created_at, replies FROM tickets"
        ))
    connection.rollback()


def test_legacy_knowledge_without_fingerprint_is_upgraded_without_rewriting_content(migration_env):
    config, connection = migration_env
    _legacy_schema(connection, fingerprint=False)
    command.stamp(config, OLD_REVISION)
    command.upgrade(config, "head")
    assert connection.execute(sa.text(
        "SELECT id, content, fingerprint FROM knowledge_docs"
    )).all() == [("k-kept", "正文", None)]
    assert compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []


def test_missing_fingerprint_index_is_restored(migration_env):
    config, connection = migration_env
    _legacy_schema(connection)
    connection.exec_driver_sql("DROP INDEX ix_knowledge_docs_fingerprint")
    connection.commit()
    command.upgrade(config, "head")
    indexes = sa.inspect(connection).get_indexes("knowledge_docs")
    assert any(i["name"] == "ix_knowledge_docs_fingerprint" and not i["unique"] for i in indexes)


def test_duplicate_dedup_keys_fail_before_schema_changes_or_version_advance(migration_env):
    config, connection = migration_env
    _legacy_schema(connection, fingerprint=False)
    connection.execute(sa.text(
        "INSERT INTO tickets SELECT 'T-duplicate', dedup_key, user_id, user_name, "
        "description, tried_steps, evidence, status, created_at, replies FROM tickets"
    ))
    connection.commit()
    command.stamp(config, OLD_REVISION)
    with pytest.raises(RuntimeError, match="重复"):
        command.upgrade(config, "head")
    assert _version(connection) == OLD_REVISION
    assert connection.scalar(sa.text("SELECT COUNT(*) FROM tickets")) == 2
    assert "fingerprint" not in {c["name"] for c in sa.inspect(connection).get_columns("knowledge_docs")}


def test_incompatible_schema_is_rejected_without_creating_business_tables(migration_env):
    config, connection = migration_env
    connection.exec_driver_sql("CREATE TABLE tickets (id VARCHAR PRIMARY KEY)")
    connection.commit()
    with pytest.raises(RuntimeError, match="不兼容"):
        command.upgrade(config, "head")
    assert _version(connection) != HEAD_REVISION
    assert set(sa.inspect(connection).get_table_names()) <= {"tickets", "alembic_version"}


def test_baseline_downgrade_is_refused_without_losing_data_or_version(migration_env):
    config, connection = migration_env
    _legacy_schema(connection)
    command.upgrade(config, "head")
    with pytest.raises(RuntimeError, match="不支持.*回退"):
        command.downgrade(config, "base")
    assert _version(connection) == HEAD_REVISION
    assert connection.scalar(sa.text("SELECT COUNT(*) FROM tickets")) == 1


def test_injected_connection_has_priority_and_remains_open(migration_env, monkeypatch):
    config, connection = migration_env

    class ForbiddenEngine:
        def connect(self):
            pytest.fail("Migration ignored its injected connection")

    monkeypatch.setattr(database, "engine", ForbiddenEngine())
    command.upgrade(config, "head")
    assert not connection.closed
    assert _version(connection) == HEAD_REVISION


def test_default_connection_uses_application_engine(migration_env):
    config, connection = migration_env
    del config.attributes["connection"]
    command.upgrade(config, "head")
    assert _version(connection) == HEAD_REVISION


def test_existing_application_schema_needs_no_table_rebuild(migration_env):
    config, connection = migration_env
    database.Base.metadata.create_all(connection)
    connection.commit()
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    sa.event.listen(connection, "before_cursor_execute", record)
    command.upgrade(config, "head")
    assert _version(connection) == HEAD_REVISION
    assert not any("_alembic_tmp" in statement for statement in statements)
    assert compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []


def test_offline_mode_is_explicitly_unsupported(migration_env):
    config, connection = migration_env
    with pytest.raises(RuntimeError, match="不支持.*离线"):
        command.upgrade(config, "head", sql=True)
    assert sa.inspect(connection).get_table_names() == []


def test_partial_unique_index_does_not_replace_full_dedup_constraint(migration_env):
    config, connection = migration_env
    _legacy_schema(connection)
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX partial_dedup ON tickets (dedup_key) WHERE status = 'open'"
    )
    connection.commit()
    command.upgrade(config, "head")
    connection.execute(sa.text("UPDATE tickets SET status = 'resolved'"))
    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(sa.text(
            "INSERT INTO tickets SELECT 'T-duplicate', dedup_key, user_id, user_name, "
            "description, tried_steps, evidence, status, created_at, replies FROM tickets"
        ))
    connection.rollback()


@pytest.mark.parametrize("structure", ["trigger", "check"])
def test_custom_structure_is_rejected_before_table_rebuild(migration_env, structure):
    config, connection = migration_env
    _legacy_schema(connection, check=structure == "check")
    if structure == "trigger":
        connection.exec_driver_sql(
            "CREATE TRIGGER keep_description BEFORE UPDATE OF description ON tickets "
            "BEGIN SELECT RAISE(ABORT, 'description is immutable'); END"
        )
    connection.commit()
    command.stamp(config, OLD_REVISION)
    before = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'tickets' ORDER BY name"
    ).all()
    with pytest.raises(RuntimeError, match="不兼容|自定义"):
        command.upgrade(config, "head")
    assert connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE tbl_name = 'tickets' ORDER BY name"
    ).all() == before
    assert _version(connection) == OLD_REVISION


def test_conflicting_index_definition_is_rejected_before_version_advance(migration_env):
    config, connection = migration_env
    _legacy_schema(connection)
    connection.exec_driver_sql("DROP INDEX ix_knowledge_docs_fingerprint")
    connection.exec_driver_sql("CREATE INDEX ix_knowledge_docs_fingerprint ON knowledge_docs (title)")
    connection.commit()
    command.stamp(config, OLD_REVISION)
    with pytest.raises(RuntimeError, match="索引.*不兼容"):
        command.upgrade(config, "head")
    assert _version(connection) == OLD_REVISION


def test_previous_head_upgrade_adds_lease_table_without_changing_knowledge(migration_env):
    config, connection = migration_env
    command.upgrade(config, "e5b7a91c2046")
    assert not sa.inspect(connection).has_table("knowledge_ingest_leases")
    connection.execute(sa.text(
        "INSERT INTO knowledge_docs (id, kind, title, keywords, content) "
        "VALUES ('k-old', 'manual', '原资料', '[]', '原正文')"
    ))
    connection.commit()
    command.upgrade(config, "head")
    assert _version(connection) == HEAD_REVISION
    assert connection.scalar(sa.text("SELECT content FROM knowledge_docs WHERE id='k-old'")) == "原正文"
    assert sa.inspect(connection).has_table("knowledge_ingest_leases")
    assert compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []


def test_incompatible_existing_lease_table_does_not_advance_revision(migration_env):
    config, connection = migration_env
    command.upgrade(config, "e5b7a91c2046")
    connection.exec_driver_sql("CREATE TABLE knowledge_ingest_leases (id INTEGER PRIMARY KEY)")
    connection.commit()
    with pytest.raises(RuntimeError, match="不兼容"):
        command.upgrade(config, "head")
    assert _version(connection) == "e5b7a91c2046"
