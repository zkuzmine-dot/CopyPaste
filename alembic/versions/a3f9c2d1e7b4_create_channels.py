"""create channels

Revision ID: a3f9c2d1e7b4
Revises: 4231a65cc226
Create Date: 2026-11-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f9c2d1e7b4"
down_revision: str | Sequence[str] | None = "4231a65cc226"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_channels_slug"), "channels", ["slug"], unique=True)
    op.create_index(op.f("ix_channels_last_activity_at"), "channels", ["last_activity_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_channels_last_activity_at"), table_name="channels")
    op.drop_index(op.f("ix_channels_slug"), table_name="channels")
    op.drop_table("channels")
