from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import Channel, Message
from app.db.models.file import StoredFile
from app.schemas.channel import MessageRead
from app.schemas.file import LinkedFileRead
from app.services import channels as channels_service
from app.services import files as files_service


def message_read(message: Message, stored: StoredFile | None, *, with_links: bool) -> MessageRead:
    file = None
    if stored is not None:
        links_expire = int(message.expires_at.timestamp()) if with_links else None
        file = LinkedFileRead.build(stored, links_expire=links_expire)
    return MessageRead(
        id=message.id,
        kind=message.kind,
        text=message.text,
        file=file,
        created_at=message.created_at,
        expires_at=message.expires_at,
    )


async def live_messages_read(
    db: AsyncSession, channel: Channel, *, viewer_id: int
) -> list[MessageRead]:
    messages = await channels_service.get_live_messages(db, channel)
    # Сервис файлов пока синхронный — через мост run_sync
    files = await db.run_sync(files_service.files_of_messages, [m.id for m in messages])
    # Ссылки на свой файл не выдаём: скачав его, отправитель удалил бы файл у получателя
    return [message_read(m, files.get(m.id), with_links=m.sender_id != viewer_id) for m in messages]
