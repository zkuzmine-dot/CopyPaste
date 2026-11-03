"""add created_at and updated_at to notes

Revision ID: 7b1e5d0a9c32
Revises: 3f8a2c91d4e7
Create Date: 2026-11-03 12:01:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b1e5d0a9c32'
down_revision: Union[str, Sequence[str], None] = '3f8a2c91d4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default заполнит существующие строки текущим временем
    op.add_column(
        "notes",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "notes",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("notes", "updated_at")
    op.drop_column("notes", "created_at")
