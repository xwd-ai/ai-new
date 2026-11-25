"""将 SQLite 业务数据复制到预先升级的空 MySQL 库。"""

import argparse
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import URL

from backend import models  # noqa: F401
from backend.database import Base, engine


def import_data(source: Path) -> dict[str, int]:
    if engine.dialect.name != "mysql":
        raise RuntimeError("导入目标必须为 MySQL，请设置 DB_DRIVER=mysql 或 DATABASE_URL。")
    source = source.resolve(strict=True)
    sqlite = sa.create_engine(URL.create(
        "sqlite", database=source.as_uri(), query={"mode": "ro", "uri": "true"},
    ))
    try:
        with sqlite.connect() as origin, engine.begin() as target:
            origin.exec_driver_sql("BEGIN")
            source_schema = sa.inspect(origin)
            target_schema = sa.inspect(target)
            tables = Base.metadata.sorted_tables
            for table in tables:
                if not target_schema.has_table(table.name):
                    raise RuntimeError(f"目标库缺少 {table.name}，请先运行 Alembic 升级。")
                if target.execute(sa.select(sa.literal(1)).select_from(table).limit(1)).first():
                    raise RuntimeError(f"目标表 {table.name} 已有数据，导入需要空库。")

            counts = {}
            for table in tables:
                if not source_schema.has_table(table.name):
                    continue
                column_names = {item["name"] for item in source_schema.get_columns(table.name)}
                missing = [column.name for column in table.columns
                           if column.name not in column_names and not column.nullable]
                if missing:
                    raise RuntimeError(f"源表 {table.name} 缺少必填列：{', '.join(missing)}")
                columns = [column for column in table.columns if column.name in column_names]
                count = 0
                for batch in origin.execute(sa.select(*columns)).mappings().partitions(200):
                    target.execute(sa.insert(table), [dict(row) for row in batch])
                    count += len(batch)
                actual = target.scalar(sa.select(sa.func.count()).select_from(table))
                if count != actual:
                    raise RuntimeError(f"表 {table.name} 导入行数不匹配。")
                counts[table.name] = count
            return counts
    finally:
        sqlite.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    for table, count in import_data(args.source).items():
        print(f"{table}: {count}")


if __name__ == "__main__":
    main()
