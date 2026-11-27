"""将工单关联到提交它的客户会话。"""

from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e6"
down_revision = "a78d6ef01142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("conversation_tickets"):
        return
    op.create_table(
        "conversation_tickets",
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ticket_id", sa.String(64), sa.ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    raise RuntimeError("此迁移不支持自动回退，以保留已保存的会话工单关联。")
