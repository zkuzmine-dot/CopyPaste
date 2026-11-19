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


def make_file(db, *, size: int = 1, origin: FileOrigin = FileOrigin.NOTE) -> StoredFile:
    return files_service.create_upload(
        db,
        storage_key=new_storage_key(),
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=size,
        origin=origin,
    )


def expire(db, stored: StoredFile) -> None:
    db.execute(
        update(StoredFile)
        .where(StoredFile.id == stored.id)
        .values(keep_until=func.now() - timedelta(seconds=1))
    )


def give_owner(db, stored: StoredFile, *, note=None, message=None) -> None:
    stored.note_id = note.id if note else None
    stored.message_id = message.id if message else None
    stored.keep_until = None
    db.flush()


@pytest.fixture
def note(db):
    return notes_service.create_note(db, NoteCreate(text="hello"))


@pytest.fixture
def message(db):
    receiver, _ = channels_service.create_channel(db)
    sender, _ = channels_service.join_channel(db, receiver.channel.slug)
    return channels_service.add_message(db, sender, "hi")


# --- создание и поиск ---


def test_create_upload_waits_for_owner(db):
    stored = make_file(db)

    assert len(stored.public_id) == 22
    assert stored.origin == FileOrigin.NOTE
    assert stored.note_id is None
    assert stored.message_id is None
    assert stored.delete_after_download is False
    assert stored.keep_until - stored.created_at == timedelta(
        seconds=settings.PENDING_UPLOAD_TTL_SECONDS
    )


def test_total_size(db):
    assert files_service.total_size(db) == 0

    make_file(db, size=3)
    make_file(db, size=5)

    assert files_service.total_size(db) == 8


def test_pending_file_is_available(db):
    stored = make_file(db)

    assert files_service.get_available_file(db, stored.public_id) is stored


def test_expired_pending_file_is_unavailable(db):
    stored = make_file(db)
    expire(db, stored)

    assert files_service.get_available_file(db, stored.public_id) is None


def test_owned_file_is_available_without_keep_until(db, note):
    stored = make_file(db)
    give_owner(db, stored, note=note)

    assert files_service.get_available_file(db, stored.public_id) is stored


def test_unknown_file_is_unavailable(db):
    assert files_service.get_available_file(db, "nope") is None


def test_delete_received_one_time_file_once(db):
    stored = make_file(db)
    stored.delete_after_download = True
    db.flush()
    key, public_id = stored.storage_key, stored.public_id

    assert files_service.delete_received(db, public_id) == files_service.DeletedFile(
        storage_key=key, message_id=None, channel_slug=None
    )
    assert files_service.delete_received(db, public_id) is None


def test_delete_received_keeps_regular_file(db, note):
    stored = make_file(db)
    give_owner(db, stored, note=note)

    assert files_service.delete_received(db, stored.public_id) is None
    assert db.scalar(select(StoredFile.id).where(StoredFile.id == stored.id)) == stored.id


def test_delete_received_channel_file_removes_message(db, message):
    stored = make_file(db, origin=FileOrigin.CHANNEL)
    give_owner(db, stored, message=message)
    stored.delete_after_download = True
    db.flush()
    message_id = message.id
    slug = db.scalar(select(Channel.slug).where(Channel.id == message.channel_id))

    deleted = files_service.delete_received(db, stored.public_id)

    assert deleted == files_service.DeletedFile(stored.storage_key, message_id, slug)
    assert db.scalar(select(Message).where(Message.id == message_id)) is None


def test_file_of_expired_message_is_unavailable(db, message):
    stored = make_file(db, origin=FileOrigin.CHANNEL)
    give_owner(db, stored, message=message)
    db.execute(
        update(Message)
        .where(Message.id == message.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert files_service.get_available_file(db, stored.public_id) is None


# --- сборщик ---


def test_collect_garbage(db, note, message):
    expired = make_file(db)
    expire(db, expired)
    without_keep_until = make_file(db)
    give_owner(db, without_keep_until)
    fresh = make_file(db)
    in_note = make_file(db)
    give_owner(db, in_note, note=note)
    in_message = make_file(db, origin=FileOrigin.CHANNEL)
    give_owner(db, in_message, message=message)
    garbage_keys = {expired.storage_key, without_keep_until.storage_key}

    assert set(files_service.collect_garbage(db)) == garbage_keys
    assert set(db.scalars(select(StoredFile.id))) == {fresh.id, in_note.id, in_message.id}


def test_collect_garbage_in_batches(db):
    for _ in range(3):
        expire(db, make_file(db))

    assert len(files_service.collect_garbage(db, limit=2)) == 2
    assert len(files_service.collect_garbage(db, limit=2)) == 1
    assert files_service.collect_garbage(db, limit=2) == []


def test_deleted_note_leaves_file_for_collector(db, note):
    stored = make_file(db)
    give_owner(db, stored, note=note)

    notes_service.delete_note(db, note)
    db.refresh(stored)

    assert stored.note_id is None
    assert files_service.collect_garbage(db) == [stored.storage_key]


def test_deleted_channel_leaves_file_for_collector(db, message):
    stored = make_file(db, origin=FileOrigin.CHANNEL)
    give_owner(db, stored, message=message)

    # Канал удаляет сообщения каскадом, а у файла обнуляется владелец
    db.execute(delete(Channel))
    db.refresh(stored)

    assert stored.message_id is None
    assert files_service.collect_garbage(db) == [stored.storage_key]


def test_known_keys(db):
    stored = make_file(db)

    assert files_service.known_keys(db, [stored.storage_key, new_storage_key()]) == {
        stored.storage_key
    }
