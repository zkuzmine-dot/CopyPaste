"""add slug to notes

Revision ID: 3f8a2c91d4e7
Revises: 0d1e1056b1ea
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f8a2c91d4e7'
down_revision: Union[str, Sequence[str], None] = '0d1e1056b1ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Сначала nullable, чтобы существующие строки не сломали NOT NULL
    op.add_column("notes", sa.Column("slug", sa.String(length=8), nullable=True))
    # Заполняем slug у уже существующих заметок
    op.execute(
        "UPDATE notes SET slug = substr(md5(random()::text || id::text), 1, 8) "
        "WHERE slug IS NULL"
    )
    op.alter_column("notes", "slug", nullable=False)
    op.create_index(op.f("ix_notes_slug"), "notes", ["slug"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_notes_slug"), table_name="notes")
    op.drop_column("notes", "slug")
