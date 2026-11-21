from datetime import timedelta

import anyio
import pytest
from sqlalchemy import func, select, update

from app.db.models.channel import Channel, Message, Participant, ParticipantRole
from app.db.models.file import FileOrigin, StoredFile
from app.services import channels as channels_service
from app.services import files as files_service
from app.settings import settings
from app.storage.base import new_storage_key

pytestmark = pytest.mark.anyio

# Гонки видны только между настоящими параллельными транзакциями,
# поэтому здесь реальные коммиты вместо общей транзакции с откатом из conftest

RACE_WINDOW = 0.3


@pytest.fixture
def factory(committing_session_factory):
    return committing_session_factory


async def create_committed_channel(factory) -> str:
    async with factory() as db, db.begin():
        participant, _ = await channels_service.create_channel(db)
        return participant.channel.slug


async def load_participant(db, slug: str, role: ParticipantRole) -> Participant:
    stmt = select(Participant).join(Channel).where(Channel.slug == slug, Participant.role == role)
    return (await db.execute(stmt)).scalar_one()


async def test_concurrent_joins_take_only_one_slot(factory, monkeypatch):
    slug = await create_committed_channel(factory)

    # Растягиваем окно между подсчётом участников и вставкой: без FOR UPDATE
    # оба join увидели бы одного участника и оба заняли бы слот
    original_count = channels_service.count_participants

    async def slow_count(db, channel):
        result = await original_count(db, channel)
        await anyio.sleep(RACE_WINDOW)
        return result

    monkeypatch.setattr(channels_service, "count_participants", slow_count)
    results: list[str] = []

    async def join() -> None:
        try:
            async with factory() as db, db.begin():
                await channels_service.join_channel(db, slug)
            results.append("joined")
        except channels_service.ChannelFullError:
            results.append("full")

    async with anyio.create_task_group() as group:
        group.start_soon(join)
        group.start_soon(join)

    assert sorted(results) == ["full", "joined"]
    async with factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(Participant).join(Channel).where(Channel.slug == slug)
        )
    assert count == 2


async def test_send_during_role_swap_is_rejected(factory, monkeypatch):
    slug = await create_committed_channel(factory)
    async with factory() as db, db.begin():
        await channels_service.join_channel(db, slug)

    # become_sender держит блокировку канала, пока «спит» в _touch, — в это время
    # старый отправитель пытается отправить. Он должен дождаться коммита
    # и увидеть, что уже стал получателем
    swap_holds_lock = anyio.Event()
    original_touch = channels_service._touch

    async def slow_touch(db, channel_id):
        swap_holds_lock.set()
        await anyio.sleep(RACE_WINDOW)
        await original_touch(db, channel_id)

    monkeypatch.setattr(channels_service, "_touch", slow_touch)

    async def swap() -> None:
        async with factory() as db, db.begin():
            receiver = await load_participant(db, slug, ParticipantRole.RECEIVER)
            await channels_service.become_sender(db, receiver)

    result: list[str] = []

    async def send() -> None:
        async with factory() as db:
            # Загружаем отправителя до смены ролей — в его объекте ещё роль sender.
            # expire_on_commit=False: после коммита объект не перечитывается сам
            old_sender = await load_participant(db, slug, ParticipantRole.SENDER)
            await db.commit()
            await swap_holds_lock.wait()
            try:
                async with db.begin():
                    await channels_service.add_message(db, old_sender, "too late")
                result.append("sent")
            except channels_service.ActionNotAllowedError:
                result.append("rejected")

    async with anyio.create_task_group() as group:
        group.start_soon(send)
        group.start_soon(swap)

    assert result == ["rejected"]
    async with factory() as db:
        messages = await db.scalars(select(Message).join(Channel).where(Channel.slug == slug))
        assert messages.all() == []


async def test_cleanup_waits_for_message_in_progress(factory, monkeypatch):
    slug = await create_committed_channel(factory)
    async with factory() as db, db.begin():
        await channels_service.join_channel(db, slug)

    # Канал за 0,2 с до таймаута. Отправка проверила активность и «спит» в _touch,
    # а за это время старая версия строки становится бездействующей. Без блокировки
    # чистка удалила бы канал из-под отправки
    monkeypatch.setattr(settings, "CHANNEL_IDLE_TIMEOUT_SECONDS", 1)
    async with factory() as db, db.begin():
        await db.execute(
            update(Channel)
            .where(Channel.slug == slug)
            .values(last_activity_at=func.now() - timedelta(seconds=0.8))
        )

    send_holds_lock = anyio.Event()
    original_touch = channels_service._touch

    async def slow_touch(db, channel_id):
        send_holds_lock.set()
        await anyio.sleep(RACE_WINDOW + 0.3)
        await original_touch(db, channel_id)

    monkeypatch.setattr(channels_service, "_touch", slow_touch)

    async def send() -> None:
        async with factory() as db, db.begin():
            sender = await load_participant(db, slug, ParticipantRole.SENDER)
            await channels_service.add_message(db, sender, "just in time")

    deleted: list[int] = []

    async def cleanup() -> None:
        await send_holds_lock.wait()
        await anyio.sleep(RACE_WINDOW)
        async with factory() as db, db.begin():
            deleted.append(await channels_service.delete_inactive_channels(db))

    async with anyio.create_task_group() as group:
        group.start_soon(send)
        group.start_soon(cleanup)

    assert deleted == [0]
    async with factory() as db:
        texts = await db.scalars(select(Message.text).join(Channel).where(Channel.slug == slug))
        assert texts.all() == ["just in time"]


async def test_send_file_during_role_swap_is_rejected(factory, monkeypatch):
    slug = await create_committed_channel(factory)
    async with factory() as db, db.begin():
        sender, _ = await channels_service.join_channel(db, slug)
        stored = await db.run_sync(
            files_service.create_upload,
            storage_key=new_storage_key(),
            name="a.txt",
            content_type="application/octet-stream",
            size_bytes=1,
            origin=FileOrigin.CHANNEL,
            uploaded_by_id=sender.id,
        )
        file_id = stored.public_id

    # То же, что с текстом: файл проходит через ту же блокировку канала и перечитывание роли
    swap_holds_lock = anyio.Event()
    original_touch = channels_service._touch

    async def slow_touch(db, channel_id):
        swap_holds_lock.set()
        await anyio.sleep(RACE_WINDOW)
        await original_touch(db, channel_id)

    monkeypatch.setattr(channels_service, "_touch", slow_touch)

    async def swap() -> None:
        async with factory() as db, db.begin():
            receiver = await load_participant(db, slug, ParticipantRole.RECEIVER)
            await channels_service.become_sender(db, receiver)

    result: list[str] = []

    async def send() -> None:
        async with factory() as db:
            old_sender = await load_participant(db, slug, ParticipantRole.SENDER)
            await db.commit()
            await swap_holds_lock.wait()
            try:
                async with db.begin():
                    await channels_service.add_file_message(db, old_sender, file_id)
                result.append("sent")
            except channels_service.ActionNotAllowedError:
                result.append("rejected")

    async with anyio.create_task_group() as group:
        group.start_soon(send)
        group.start_soon(swap)

    assert result == ["rejected"]
    async with factory() as db:
        messages = await db.scalars(select(Message).join(Channel).where(Channel.slug == slug))
        assert messages.all() == []
        file = await db.scalar(select(StoredFile).where(StoredFile.public_id == file_id))
        assert file.message_id is None
