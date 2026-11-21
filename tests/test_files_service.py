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


async def make_file(async_db, *, size: int = 1, origin: FileOrigin = FileOrigin.NOTE) -> StoredFile:
    return await async_db.run_sync(
        files_service.create_upload,
        storage_key=new_storage_key(),
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=size,
        origin=origin,
    )


async def expire(async_db, stored: StoredFile) -> None:
    await async_db.execute(
        update(StoredFile)
        .where(StoredFile.id == stored.id)
        .values(keep_until=func.now() - timedelta(seconds=1))
    )


async def give_owner(async_db, stored: StoredFile, *, note=None, message=None) -> None:
    stored.note_id = note.id if note else None
    stored.message_id = message.id if message else None
    stored.keep_until = None
    await async_db.flush()


@pytest.fixture
async def note(async_db):
    return await notes_service.create_note(async_db, NoteCreate(text="hello"))


@pytest.fixture
async def message(async_db):
    receiver, _ = await channels_service.create_channel(async_db)
    sender, _ = await channels_service.join_channel(async_db, receiver.channel.slug)
    return await channels_service.add_message(async_db, sender, "hi")


# --- создание и поиск ---


async def test_create_upload_waits_for_owner(async_db):
    stored = await make_file(async_db)

    assert len(stored.public_id) == 22
    assert stored.origin == FileOrigin.NOTE
    assert stored.note_id is None
    assert stored.message_id is None
    assert stored.delete_after_download is False
    assert stored.keep_until - stored.created_at == timedelta(
        seconds=settings.PENDING_UPLOAD_TTL_SECONDS
    )


async def test_total_size(async_db):
    assert await async_db.run_sync(files_service.total_size) == 0

    await make_file(async_db, size=3)
    await make_file(async_db, size=5)

    assert await async_db.run_sync(files_service.total_size) == 8


async def test_pending_file_is_available(async_db):
    stored = await make_file(async_db)

    assert await async_db.run_sync(files_service.get_available_file, stored.public_id) is stored


async def test_expired_pending_file_is_unavailable(async_db):
    stored = await make_file(async_db)
    await expire(async_db, stored)

    assert await async_db.run_sync(files_service.get_available_file, stored.public_id) is None


async def test_owned_file_is_available_without_keep_until(async_db, note):
    stored = await make_file(async_db)
    await give_owner(async_db, stored, note=note)

    assert await async_db.run_sync(files_service.get_available_file, stored.public_id) is stored


async def test_unknown_file_is_unavailable(async_db):
    assert await async_db.run_sync(files_service.get_available_file, "nope") is None


async def test_delete_received_one_time_file_once(async_db):
    stored = await make_file(async_db)
    stored.delete_after_download = True
    await async_db.flush()
    key, public_id = stored.storage_key, stored.public_id

    assert await async_db.run_sync(
        files_service.delete_received, public_id
    ) == files_service.DeletedFile(storage_key=key, message_id=None, channel_slug=None)
    assert await async_db.run_sync(files_service.delete_received, public_id) is None


async def test_delete_received_keeps_regular_file(async_db, note):
    stored = await make_file(async_db)
    await give_owner(async_db, stored, note=note)

    assert await async_db.run_sync(files_service.delete_received, stored.public_id) is None
    assert (
        await async_db.scalar(select(StoredFile.id).where(StoredFile.id == stored.id)) == stored.id
    )


async def test_delete_received_channel_file_removes_message(async_db, message):
    stored = await make_file(async_db, origin=FileOrigin.CHANNEL)
    await give_owner(async_db, stored, message=message)
    stored.delete_after_download = True
    await async_db.flush()
    message_id = message.id
    slug = await async_db.scalar(select(Channel.slug).where(Channel.id == message.channel_id))

    deleted = await async_db.run_sync(files_service.delete_received, stored.public_id)

    assert deleted == files_service.DeletedFile(stored.storage_key, message_id, slug)
    assert await async_db.scalar(select(Message).where(Message.id == message_id)) is None


async def test_file_of_expired_message_is_unavailable(async_db, message):
    stored = await make_file(async_db, origin=FileOrigin.CHANNEL)
    await give_owner(async_db, stored, message=message)
    await async_db.execute(
        update(Message)
        .where(Message.id == message.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert await async_db.run_sync(files_service.get_available_file, stored.public_id) is None


# --- сборщик ---


async def test_collect_garbage(async_db, note, message):
    expired = await make_file(async_db)
    await expire(async_db, expired)
    without_keep_until = await make_file(async_db)
    await give_owner(async_db, without_keep_until)
    fresh = await make_file(async_db)
    in_note = await make_file(async_db)
    await give_owner(async_db, in_note, note=note)
    in_message = await make_file(async_db, origin=FileOrigin.CHANNEL)
    await give_owner(async_db, in_message, message=message)
    garbage_keys = {expired.storage_key, without_keep_until.storage_key}

    assert set(await async_db.run_sync(files_service.collect_garbage)) == garbage_keys
    assert set(await async_db.scalars(select(StoredFile.id))) == {
        fresh.id,
        in_note.id,
        in_message.id,
    }


async def test_collect_garbage_in_batches(async_db):
    for _ in range(3):
        await expire(async_db, await make_file(async_db))

    assert len(await async_db.run_sync(files_service.collect_garbage, limit=2)) == 2
    assert len(await async_db.run_sync(files_service.collect_garbage, limit=2)) == 1
    assert await async_db.run_sync(files_service.collect_garbage, limit=2) == []


async def test_deleted_note_leaves_file_for_collector(async_db, note):
    stored = await make_file(async_db)
    await give_owner(async_db, stored, note=note)

    await notes_service.delete_note(async_db, note)
    await async_db.refresh(stored)

    assert stored.note_id is None
    assert await async_db.run_sync(files_service.collect_garbage) == [stored.storage_key]


async def test_deleted_channel_leaves_file_for_collector(async_db, message):
    stored = await make_file(async_db, origin=FileOrigin.CHANNEL)
    await give_owner(async_db, stored, message=message)

    # Канал удаляет сообщения каскадом, а у файла обнуляется владелец
    await async_db.execute(delete(Channel))
    await async_db.refresh(stored)

    assert stored.message_id is None
    assert await async_db.run_sync(files_service.collect_garbage) == [stored.storage_key]


async def test_known_keys(async_db):
    stored = await make_file(async_db)

    assert await async_db.run_sync(
        files_service.known_keys, [stored.storage_key, new_storage_key()]
    ) == {stored.storage_key}
