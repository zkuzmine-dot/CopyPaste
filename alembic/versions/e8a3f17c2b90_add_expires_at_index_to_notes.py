"""add partial index on notes.expires_at

Revision ID: e8a3f17c2b90
Revises: c4d9e2f6a815
Create Date: 2026-11-04 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8a3f17c2b90'
down_revision: Union[str, Sequence[str], None] = 'c4d9e2f6a815'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "ix_notes_expires_at",
        "notes",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_notes_expires_at", table_name="notes")
