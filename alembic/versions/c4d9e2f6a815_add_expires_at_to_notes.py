"""add expires_at to notes

Revision ID: c4d9e2f6a815
Revises: 7b1e5d0a9c32
Create Date: 2026-11-03 12:02:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d9e2f6a815'
down_revision: Union[str, Sequence[str], None] = '7b1e5d0a9c32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "notes",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("notes", "expires_at")
