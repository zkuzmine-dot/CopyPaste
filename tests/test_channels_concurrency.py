import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import sessionmaker

from app.db.models.channel import Channel, Message, Participant, ParticipantRole
from app.db.models.file import FileOrigin, StoredFile
from app.services import channels as channels_service
from app.services import files as files_service
from app.settings import settings
from app.storage.base import new_storage_key

# Гонки видны только между настоящими параллельными транзакциями,
# поэтому здесь реальные коммиты вместо общей транзакции с откатом из conftest

RACE_WINDOW = 0.3


@pytest.fixture
def committed(engine):
    factory = sessionmaker(bind=engine)
    created_channel_ids: list[int] = []
    yield factory, created_channel_ids
    with factory.begin() as db:
        db.execute(delete(Channel).where(Channel.id.in_(created_channel_ids)))


def create_committed_channel(factory, created_channel_ids) -> str:
    with factory.begin() as db:
        participant, _ = channels_service.create_channel(db)
        created_channel_ids.append(participant.channel_id)
        return participant.channel.slug


def test_concurrent_joins_take_only_one_slot(committed, monkeypatch):
    factory, created_channel_ids = committed
    slug = create_committed_channel(factory, created_channel_ids)

    # Растягиваем окно между подсчётом участников и вставкой: без FOR UPDATE
    # оба потока увидели бы одного участника и оба заняли бы слот
    original_count = channels_service.count_participants

    def slow_count(db, channel):
        result = original_count(db, channel)
        time.sleep(RACE_WINDOW)
        return result

    monkeypatch.setattr(channels_service, "count_participants", slow_count)
    barrier = threading.Barrier(2)

    def join() -> str:
        barrier.wait()
        try:
            with factory.begin() as db:
                channels_service.join_channel(db, slug)
            return "joined"
        except channels_service.ChannelFullError:
            return "full"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _: join(), range(2)))

    assert results == ["full", "joined"]
    with factory() as db:
        count = db.scalar(
            select(func.count()).select_from(Participant).join(Channel).where(Channel.slug == slug)
        )
    assert count == 2


def test_send_during_role_swap_is_rejected(committed, monkeypatch):
    factory, created_channel_ids = committed
    slug = create_committed_channel(factory, created_channel_ids)
    with factory.begin() as db:
        channels_service.join_channel(db, slug)

    # become_sender держит блокировку канала, пока «спит» в _touch, — в это время
    # старый отправитель пытается отправить. Он должен дождаться коммита
    # и увидеть, что уже стал получателем
    swap_holds_lock = threading.Event()
    original_touch = channels_service._touch

    def slow_touch(db, channel_id):
        swap_holds_lock.set()
        time.sleep(RACE_WINDOW)
        original_touch(db, channel_id)

    monkeypatch.setattr(channels_service, "_touch", slow_touch)

    def load_participant(db, role: ParticipantRole) -> Participant:
        return db.execute(
            select(Participant).join(Channel).where(Channel.slug == slug, Participant.role == role)
        ).scalar_one()

    def swap() -> None:
        with factory.begin() as db:
            channels_service.become_sender(db, load_participant(db, ParticipantRole.RECEIVER))

    def send() -> str:
        with factory() as db:
            # Загружаем отправителя до смены ролей — в его объекте ещё роль sender
            old_sender = load_participant(db, ParticipantRole.SENDER)
            db.commit()
            swap_holds_lock.wait()
            try:
                with db.begin():
                    channels_service.add_message(db, old_sender, "too late")
                return "sent"
            except channels_service.ActionNotAllowedError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        send_result = pool.submit(send)
        pool.submit(swap).result()

    assert send_result.result() == "rejected"
    with factory() as db:
        assert db.scalars(select(Message).join(Channel).where(Channel.slug == slug)).all() == []


def test_cleanup_waits_for_message_in_progress(committed, monkeypatch):
    factory, created_channel_ids = committed
    slug = create_committed_channel(factory, created_channel_ids)
    with factory.begin() as db:
        channels_service.join_channel(db, slug)

    # Канал за 0,2 с до таймаута. Отправка проверила активность и «спит» в _touch,
    # а за это время старая версия строки становится бездействующей. Без блокировки
    # чистка удалила бы канал из-под отправки
    monkeypatch.setattr(settings, "CHANNEL_IDLE_TIMEOUT_SECONDS", 1)
    with factory.begin() as db:
        db.execute(
            update(Channel)
            .where(Channel.slug == slug)
            .values(last_activity_at=func.now() - timedelta(seconds=0.8))
        )

    send_holds_lock = threading.Event()
    original_touch = channels_service._touch

    def slow_touch(db, channel_id):
        send_holds_lock.set()
        time.sleep(RACE_WINDOW + 0.3)
        original_touch(db, channel_id)

    monkeypatch.setattr(channels_service, "_touch", slow_touch)

    def send() -> None:
        with factory.begin() as db:
            sender = db.execute(
                select(Participant)
                .join(Channel)
                .where(Channel.slug == slug, Participant.role == ParticipantRole.SENDER)
            ).scalar_one()
            channels_service.add_message(db, sender, "just in time")

    def cleanup() -> int:
        send_holds_lock.wait()
        time.sleep(RACE_WINDOW)
        with factory.begin() as db:
            return channels_service.delete_inactive_channels(db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(send)
        deleted = pool.submit(cleanup)
        sent.result()

    assert deleted.result() == 0
    with factory() as db:
        assert db.scalars(select(Message.text).join(Channel).where(Channel.slug == slug)).all() == [
            "just in time"
        ]


def test_send_file_during_role_swap_is_rejected(committed, monkeypatch):
    factory, created_channel_ids = committed
    slug = create_committed_channel(factory, created_channel_ids)
    with factory.begin() as db:
        sender, _ = channels_service.join_channel(db, slug)
        file_id = files_service.create_upload(
            db,
            storage_key=new_storage_key(),
            name="a.txt",
            content_type="application/octet-stream",
            size_bytes=1,
            origin=FileOrigin.CHANNEL,
            uploaded_by_id=sender.id,
        ).public_id

    # То же, что с текстом: файл проходит через ту же блокировку канала и перечитывание роли
    swap_holds_lock = threading.Event()
    original_touch = channels_service._touch

    def slow_touch(db, channel_id):
        swap_holds_lock.set()
        time.sleep(RACE_WINDOW)
        original_touch(db, channel_id)

    monkeypatch.setattr(channels_service, "_touch", slow_touch)

    def load_participant(db, role: ParticipantRole) -> Participant:
        return db.execute(
            select(Participant).join(Channel).where(Channel.slug == slug, Participant.role == role)
        ).scalar_one()

    def swap() -> None:
        with factory.begin() as db:
            channels_service.become_sender(db, load_participant(db, ParticipantRole.RECEIVER))

    def send() -> str:
        with factory() as db:
            old_sender = load_participant(db, ParticipantRole.SENDER)
            db.commit()
            swap_holds_lock.wait()
            try:
                with db.begin():
                    channels_service.add_file_message(db, old_sender, file_id)
                return "sent"
            except channels_service.ActionNotAllowedError:
                return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            send_result = pool.submit(send)
            pool.submit(swap).result()

        assert send_result.result() == "rejected"
        with factory() as db:
            assert db.scalars(select(Message).join(Channel).where(Channel.slug == slug)).all() == []
            stored = db.scalar(select(StoredFile).where(StoredFile.public_id == file_id))
            assert stored.message_id is None
    finally:
        with factory.begin() as db:
            db.execute(delete(StoredFile).where(StoredFile.public_id == file_id))
