"""create participants

Revision ID: b81e4c0f9d27
Revises: a3f9c2d1e7b4
Create Date: 2026-11-17 12:01:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b81e4c0f9d27"
down_revision: str | Sequence[str] | None = "a3f9c2d1e7b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "participants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('sender', 'receiver')", name="ck_participants_role"),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_participants_channel_id"), "participants", ["channel_id"])
    op.create_index(op.f("ix_participants_token_hash"), "participants", ["token_hash"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_participants_token_hash"), table_name="participants")
    op.drop_index(op.f("ix_participants_channel_id"), table_name="participants")
    op.drop_table("participants")
