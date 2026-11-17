"""create messages

Revision ID: c5d2a7e93f18
Revises: b81e4c0f9d27
Create Date: 2026-11-17 12:02:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d2a7e93f18"
down_revision: str | Sequence[str] | None = "b81e4c0f9d27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("sender_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_id"], ["participants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_channel_id_created_at", "messages", ["channel_id", "created_at"])
    op.create_index(op.f("ix_messages_expires_at"), "messages", ["expires_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_messages_expires_at"), table_name="messages")
    op.drop_index("ix_messages_channel_id_created_at", table_name="messages")
    op.drop_table("messages")
