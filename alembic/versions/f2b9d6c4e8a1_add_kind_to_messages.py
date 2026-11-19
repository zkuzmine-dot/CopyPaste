"""add kind to messages

Revision ID: f2b9d6c4e8a1
Revises: d4f8b2e6a1c9
Create Date: 2026-11-18 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b9d6c4e8a1"
down_revision: str | Sequence[str] | None = "d4f8b2e6a1c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default: существующие сообщения становятся текстовыми, данные не трогаем
    op.add_column(
        "messages",
        sa.Column("kind", sa.String(length=8), server_default="text", nullable=False),
    )
    op.alter_column("messages", "text", existing_type=sa.Text(), nullable=True)
    op.create_check_constraint("ck_messages_kind", "messages", "kind IN ('text', 'file')")
    op.create_check_constraint(
        "ck_messages_text_matches_kind", "messages", "(kind = 'text') = (text IS NOT NULL)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # У файловых сообщений нет текста — со старой схемой (text NOT NULL) они несовместимы
    op.execute("DELETE FROM messages WHERE kind = 'file'")
    op.drop_constraint("ck_messages_text_matches_kind", "messages", type_="check")
    op.drop_constraint("ck_messages_kind", "messages", type_="check")
    op.alter_column("messages", "text", existing_type=sa.Text(), nullable=False)
    op.drop_column("messages", "kind")
