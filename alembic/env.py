from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
from backend.database import Base
from backend import models  # noqa: F401  导入以注册所有模型

# 迁移目标：应用自身的模型元数据。
# 这样 autogenerate 生成的就是「模型定义」与「实际库结构」的差异，
# 不必手工维护一份平行的表定义。
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """基线依赖在线结构检查，明确拒绝离线 SQL 导出。"""
    raise RuntimeError("基线迁移需要检查已有表结构，不支持 --sql 离线模式。")


def _run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在「在线」模式下执行迁移：复用应用自己的引擎。

    不另外读 alembic.ini 里的连接串，是为了保证迁移和运行时永远指向
    同一个数据库文件——两处各写一份 URL，迟早会出现「迁移跑在 A 库、
    应用连的是 B 库」这种难查的问题。
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
    else:
        from backend.database import engine

        with engine.connect() as connection:
            _run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
