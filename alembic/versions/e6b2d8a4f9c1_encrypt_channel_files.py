"""encrypt channel files

Revision ID: e6b2d8a4f9c1
Revises: d3a7f9c1e2b4
Create Date: 2026-11-24 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6b2d8a4f9c1"
down_revision: str | Sequence[str] | None = "d3a7f9c1e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _close_channels() -> None:
    # Открытые страницы шлют файлы в старом виде. Каналы живут минуты, закрытые удалит чистка
    op.execute("UPDATE channels SET closed_at = now() WHERE closed_at IS NULL")


def upgrade() -> None:
    """Upgrade schema."""
    _close_channels()
    # У прежних файловых сообщений описания нет. Файлы без сообщения подберёт сборщик
    op.execute("DELETE FROM messages WHERE payload IS NULL")
    op.drop_constraint("ck_messages_payload_matches_kind", "messages", type_="check")
    op.alter_column("messages", "payload", existing_type=sa.LargeBinary(), nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    _close_channels()
    op.alter_column("messages", "payload", existing_type=sa.LargeBinary(), nullable=True)
    op.execute("UPDATE messages SET payload = NULL WHERE kind = 'file'")
    op.create_check_constraint(
        "ck_messages_payload_matches_kind", "messages", "(kind = 'text') = (payload IS NOT NULL)"
    )
