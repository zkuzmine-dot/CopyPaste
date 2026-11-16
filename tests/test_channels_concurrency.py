import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app.db.models.channel import Channel, Message, Participant, ParticipantRole
from app.services import channels as channels_service

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
