from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models.channel import Channel, Message, Participant, ParticipantRole
from app.services import channels as channels_service
from app.settings import settings
from app.utils.tokens import hash_token

pytestmark = pytest.mark.anyio


@pytest.fixture
async def receiver(db) -> Participant:
    participant, _ = await channels_service.create_channel(db)
    return participant


@pytest.fixture
async def sender(db, receiver) -> Participant:
    participant, _ = await channels_service.join_channel(db, receiver.channel.slug)
    return participant


async def channel_of(db, participant: Participant) -> Channel:
    # Явно: связь participant.channel не догружается сама (lazy="raise"), а после refresh
    # в сервисе её в объекте уже нет
    channel = await db.get(Channel, participant.channel_id)
    assert channel is not None
    return channel


async def set_last_activity(db, channel: Channel, seconds_ago: int) -> None:
    await db.execute(
        update(Channel)
        .where(Channel.id == channel.id)
        .values(last_activity_at=func.now() - timedelta(seconds=seconds_ago))
    )


# --- create / join ---


async def test_create_channel(db):
    participant, token = await channels_service.create_channel(db)

    assert participant.role == ParticipantRole.RECEIVER
    assert participant.channel.slug
    # В базе только хэш, сам токен не хранится
    assert participant.token_hash == hash_token(token)
    assert token not in (await db.scalars(select(Participant.token_hash))).all()


async def test_join_channel(db, receiver):
    participant, token = await channels_service.join_channel(db, receiver.channel.slug)

    assert participant.role == ParticipantRole.SENDER
    assert participant.channel_id == receiver.channel_id
    assert token


async def test_join_after_creator_became_sender(db, receiver):
    await channels_service.become_sender(db, receiver)

    participant, _ = await channels_service.join_channel(db, (await channel_of(db, receiver)).slug)

    assert receiver.role == ParticipantRole.SENDER
    assert participant.role == ParticipantRole.RECEIVER


async def test_join_full_channel(db, sender):
    with pytest.raises(channels_service.ChannelFullError):
        await channels_service.join_channel(db, sender.channel.slug)


async def test_join_unknown_channel(db):
    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.join_channel(db, "zzzzzzzz")


async def test_join_closed_channel(db, receiver):
    await channels_service.close_channel(db, receiver.channel)

    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.join_channel(db, receiver.channel.slug)


# --- активность канала ---


async def test_get_active_channel(db, receiver):
    assert await channels_service.get_active_channel(db, receiver.channel.slug) is receiver.channel


async def test_closed_channel_is_not_active(db, receiver):
    await channels_service.close_channel(db, receiver.channel)

    assert await channels_service.get_active_channel(db, receiver.channel.slug) is None


async def test_idle_channel_is_not_active(db, receiver):
    await set_last_activity(db, receiver.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    assert await channels_service.get_active_channel(db, receiver.channel.slug) is None


# --- токены ---


async def test_get_participant_by_token(db):
    participant, token = await channels_service.create_channel(db)

    assert await channels_service.get_participant(db, participant.channel, token) is participant


async def test_get_participant_wrong_token(db, receiver):
    assert await channels_service.get_participant(db, receiver.channel, "wrong") is None


async def test_get_participant_token_from_other_channel(db, receiver):
    _, other_token = await channels_service.create_channel(db)

    assert await channels_service.get_participant(db, receiver.channel, other_token) is None


# --- сообщения ---


async def test_add_message(db, sender):
    message = await channels_service.add_message(db, sender, "hello")

    assert message.text == "hello"
    assert message.sender_id == sender.id
    assert message.expires_at - message.created_at == timedelta(
        seconds=settings.MESSAGE_TTL_SECONDS
    )


async def test_add_message_by_receiver_not_allowed(db, receiver, sender):
    with pytest.raises(channels_service.ActionNotAllowedError):
        await channels_service.add_message(db, receiver, "hello")

    assert (await db.scalars(select(Message))).all() == []


async def test_add_message_updates_activity(db, sender):
    channel = await channel_of(db, sender)
    await set_last_activity(db, channel, 60)

    await channels_service.add_message(db, sender, "hello")

    await db.refresh(channel)
    assert channel.last_activity_at == await db.scalar(select(func.now()))


async def test_get_live_messages_in_order(db, sender):
    first = await channels_service.add_message(db, sender, "first")
    second = await channels_service.add_message(db, sender, "second")
    channel = await channel_of(db, sender)

    assert await channels_service.get_live_messages(db, channel) == [first, second]


async def test_get_live_messages_skips_expired(db, sender):
    expired = await channels_service.add_message(db, sender, "old")
    alive = await channels_service.add_message(db, sender, "new")
    await db.execute(
        update(Message)
        .where(Message.id == expired.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert await channels_service.get_live_messages(db, await channel_of(db, sender)) == [alive]


# --- роли ---


async def test_become_sender_swaps_roles(db, receiver, sender):
    await channels_service.become_sender(db, receiver)

    assert receiver.role == ParticipantRole.SENDER
    assert sender.role == ParticipantRole.RECEIVER


async def test_become_sender_by_sender_not_allowed(db, receiver, sender):
    with pytest.raises(channels_service.ActionNotAllowedError):
        await channels_service.become_sender(db, sender)

    assert (receiver.role, sender.role) == (ParticipantRole.RECEIVER, ParticipantRole.SENDER)


async def test_new_sender_can_send_and_old_cannot(db, receiver, sender):
    await channels_service.become_sender(db, receiver)

    await channels_service.add_message(db, receiver, "from new sender")
    with pytest.raises(channels_service.ActionNotAllowedError):
        await channels_service.add_message(db, sender, "from old sender")


# --- действия в закрытом канале ---


@pytest.mark.parametrize("close", ["closed", "idle"])
async def test_actions_in_inactive_channel_not_allowed(db, receiver, sender, close):
    if close == "closed":
        await channels_service.close_channel(db, sender.channel)
    else:
        await set_last_activity(db, sender.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.add_message(db, sender, "late")
    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.become_sender(db, receiver)


# --- close_if_idle ---


async def test_close_if_idle_keeps_active_channel(db, receiver):
    await set_last_activity(db, receiver.channel, 10)

    seconds_left = await channels_service.close_if_idle(db, receiver.channel_id)

    assert seconds_left == pytest.approx(settings.CHANNEL_IDLE_TIMEOUT_SECONDS - 10, abs=1)
    await db.refresh(receiver.channel)
    assert receiver.channel.closed_at is None


async def test_close_if_idle_closes_idle_channel(db, receiver):
    await set_last_activity(db, receiver.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    assert await channels_service.close_if_idle(db, receiver.channel_id) is None
    await db.refresh(receiver.channel)
    assert receiver.channel.closed_at is not None


async def test_close_if_idle_on_closed_channel(db, receiver):
    await channels_service.close_channel(db, receiver.channel)

    assert await channels_service.close_if_idle(db, receiver.channel_id) is None


# --- чистка ---


async def test_delete_expired_messages(db, sender):
    expired = await channels_service.add_message(db, sender, "old")
    live = await channels_service.add_message(db, sender, "new")
    await db.execute(
        update(Message)
        .where(Message.id == expired.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert await channels_service.delete_expired_messages(db) == 1
    assert (await db.scalars(select(Message.id))).all() == [live.id]


async def test_delete_inactive_channels(db, sender):
    await channels_service.add_message(db, sender, "text")
    idle, _ = await channels_service.create_channel(db)
    closed, _ = await channels_service.create_channel(db)
    await set_last_activity(db, idle.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)
    await channels_service.close_channel(db, closed.channel)
    await set_last_activity(
        db, await channel_of(db, sender), settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1
    )

    assert await channels_service.delete_inactive_channels(db) == 3
    assert (await db.scalars(select(Channel.id))).all() == []
    # Каскад: участники и сообщения ушли вместе с каналами
    assert (await db.scalars(select(Participant.id))).all() == []
    assert (await db.scalars(select(Message.id))).all() == []


async def test_delete_inactive_channels_keeps_active(db, sender):
    await set_last_activity(db, sender.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS - 10)

    assert await channels_service.delete_inactive_channels(db) == 0
    assert (await db.scalars(select(func.count()).select_from(Participant))).one() == 2
