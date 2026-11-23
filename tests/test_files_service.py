from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, update

from app.db.models.channel import Channel, Message
from app.db.models.file import FileOrigin, StoredFile
from app.schemas.note import NoteCreate
from app.services import channels as channels_service
from app.services import files as files_service
from app.services import notes as notes_service
from app.settings import settings
from app.storage.base import new_storage_key

pytestmark = pytest.mark.anyio


async def make_file(db, *, size: int = 1, origin: FileOrigin = FileOrigin.NOTE) -> StoredFile:
    return await files_service.create_upload(
        db,
        storage_key=new_storage_key(),
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=size,
        origin=origin,
    )


async def expire(db, stored: StoredFile) -> None:
    await db.execute(
        update(StoredFile)
        .where(StoredFile.id == stored.id)
        .values(keep_until=func.now() - timedelta(seconds=1))
    )


async def give_owner(db, stored: StoredFile, *, note=None, message=None) -> None:
    stored.note_id = note.id if note else None
    stored.message_id = message.id if message else None
    stored.keep_until = None
    await db.flush()


@pytest.fixture
async def note(db):
    return await notes_service.create_note(db, NoteCreate(text="hello"))


@pytest.fixture
async def message(db):
    receiver, _ = await channels_service.create_channel(db)
    sender, _ = await channels_service.join_channel(db, receiver.channel.slug)
    return await channels_service.add_message(db, sender, "hi")


# --- создание и поиск ---


async def test_create_upload_waits_for_owner(db):
    stored = await make_file(db)

    assert len(stored.public_id) == 22
    assert stored.origin == FileOrigin.NOTE
    assert stored.note_id is None
    assert stored.message_id is None
    assert stored.delete_after_download is False
    assert stored.keep_until - stored.created_at == timedelta(
        seconds=settings.PENDING_UPLOAD_TTL_SECONDS
    )


async def test_total_size(db):
    assert await files_service.total_size(db) == 0

    await make_file(db, size=3)
    await make_file(db, size=5)

    assert await files_service.total_size(db) == 8


async def test_pending_file_is_available(db):
    stored = await make_file(db)

    assert await files_service.get_available_file(db, stored.public_id) is stored


async def test_expired_pending_file_is_unavailable(db):
    stored = await make_file(db)
    await expire(db, stored)

    assert await files_service.get_available_file(db, stored.public_id) is None


async def test_owned_file_is_available_without_keep_until(db, note):
    stored = await make_file(db)
    await give_owner(db, stored, note=note)

    assert await files_service.get_available_file(db, stored.public_id) is stored


async def test_unknown_file_is_unavailable(db):
    assert await files_service.get_available_file(db, "nope") is None


async def test_delete_received_one_time_file_once(db):
    stored = await make_file(db)
    stored.delete_after_download = True
    await db.flush()
    key, public_id = stored.storage_key, stored.public_id

    assert await files_service.delete_received(db, public_id) == files_service.DeletedFile(
        storage_key=key, message_id=None, channel_slug=None
    )
    assert await files_service.delete_received(db, public_id) is None


async def test_delete_received_keeps_regular_file(db, note):
    stored = await make_file(db)
    await give_owner(db, stored, note=note)

    assert await files_service.delete_received(db, stored.public_id) is None
    assert await db.scalar(select(StoredFile.id).where(StoredFile.id == stored.id)) == stored.id


async def test_delete_received_channel_file_removes_message(db, message):
    stored = await make_file(db, origin=FileOrigin.CHANNEL)
    await give_owner(db, stored, message=message)
    stored.delete_after_download = True
    await db.flush()
    message_id = message.id
    slug = await db.scalar(select(Channel.slug).where(Channel.id == message.channel_id))

    deleted = await files_service.delete_received(db, stored.public_id)

    assert deleted == files_service.DeletedFile(stored.storage_key, message_id, slug)
    assert await db.scalar(select(Message).where(Message.id == message_id)) is None


async def test_file_of_expired_message_is_unavailable(db, message):
    stored = await make_file(db, origin=FileOrigin.CHANNEL)
    await give_owner(db, stored, message=message)
    await db.execute(
        update(Message)
        .where(Message.id == message.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert await files_service.get_available_file(db, stored.public_id) is None


# --- сборщик ---


async def test_collect_garbage(db, note, message):
    expired = await make_file(db)
    await expire(db, expired)
    without_keep_until = await make_file(db)
    await give_owner(db, without_keep_until)
    fresh = await make_file(db)
    in_note = await make_file(db)
    await give_owner(db, in_note, note=note)
    in_message = await make_file(db, origin=FileOrigin.CHANNEL)
    await give_owner(db, in_message, message=message)
    garbage_keys = {expired.storage_key, without_keep_until.storage_key}

    assert set(await files_service.collect_garbage(db)) == garbage_keys
    assert set(await db.scalars(select(StoredFile.id))) == {
        fresh.id,
        in_note.id,
        in_message.id,
    }


async def test_collect_garbage_in_batches(db):
    for _ in range(3):
        await expire(db, await make_file(db))

    assert len(await files_service.collect_garbage(db, limit=2)) == 2
    assert len(await files_service.collect_garbage(db, limit=2)) == 1
    assert await files_service.collect_garbage(db, limit=2) == []


async def test_deleted_note_leaves_file_for_collector(db, note):
    stored = await make_file(db)
    await give_owner(db, stored, note=note)

    await notes_service.delete_note(db, note)
    await db.refresh(stored)

    assert stored.note_id is None
    assert await files_service.collect_garbage(db) == [stored.storage_key]


async def test_deleted_channel_leaves_file_for_collector(db, message):
    stored = await make_file(db, origin=FileOrigin.CHANNEL)
    await give_owner(db, stored, message=message)

    # Канал удаляет сообщения каскадом, а у файла обнуляется владелец
    await db.execute(delete(Channel))
    await db.refresh(stored)

    assert stored.message_id is None
    assert await files_service.collect_garbage(db) == [stored.storage_key]


async def test_known_keys(db):
    stored = await make_file(db)

    assert await files_service.known_keys(db, [stored.storage_key, new_storage_key()]) == {
        stored.storage_key
    }
