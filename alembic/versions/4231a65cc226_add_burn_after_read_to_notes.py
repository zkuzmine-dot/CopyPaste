"""add burn_after_read to notes

Revision ID: 4231a65cc226
Revises: e8a3f17c2b90
Create Date: 2026-11-16 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4231a65cc226"
down_revision: str | Sequence[str] | None = "e8a3f17c2b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default заполнит существующие заметки значением false
    op.add_column(
        "notes",
        sa.Column("burn_after_read", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("notes", "burn_after_read")
