"""encrypt channel messages

Revision ID: d3a7f9c1e2b4
Revises: c8e5a2f71d3b
Create Date: 2026-11-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a7f9c1e2b4"
down_revision: str | Sequence[str] | None = "c8e5a2f71d3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _close_channels_and_drop_texts() -> None:
    # Открытые страницы шлют текст в старом виде, а у текстовых сообщений колонка меняется
    # целиком. Каналы и сообщения живут минуты, закрытые удалит обычная чистка
    op.execute("UPDATE channels SET closed_at = now() WHERE closed_at IS NULL")
    op.execute("DELETE FROM messages WHERE kind = 'text'")


def upgrade() -> None:
    """Upgrade schema."""
    _close_channels_and_drop_texts()
    op.drop_constraint("ck_messages_text_matches_kind", "messages", type_="check")
    op.drop_column("messages", "text")
    op.add_column("messages", sa.Column("payload", sa.LargeBinary(), nullable=True))
    op.create_check_constraint(
        "ck_messages_payload_matches_kind", "messages", "(kind = 'text') = (payload IS NOT NULL)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    _close_channels_and_drop_texts()
    op.drop_constraint("ck_messages_payload_matches_kind", "messages", type_="check")
    op.drop_column("messages", "payload")
    op.add_column("messages", sa.Column("text", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_messages_text_matches_kind", "messages", "(kind = 'text') = (text IS NOT NULL)"
    )
