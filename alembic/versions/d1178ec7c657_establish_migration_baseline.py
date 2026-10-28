"""establish migration baseline

Revision ID: d1178ec7c657
Revises: 
Create Date: 2026-10-28 10:09:32.582490

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'd1178ec7c657'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """保留已登记的旧版本接入点，完整结构由后继修复迁移建立。

    旧脚本只对已有表添加匿名约束，空库与旧库均无法执行；部分旧库
    已直接登记此版本，因此结构修复不能仅靠重写此处触发。
    """


def downgrade() -> None:
    raise RuntimeError("基线不支持自动回退；如需恢复，请使用升级前的数据库备份。")
