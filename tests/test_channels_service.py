from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models.channel import Channel, Message, Participant, ParticipantRole
from app.services import channels as channels_service
from app.settings import settings
from app.utils.tokens import hash_token

pytestmark = pytest.mark.anyio


@pytest.fixture
async def receiver(async_db) -> Participant:
    participant, _ = await channels_service.create_channel(async_db)
    return participant


@pytest.fixture
async def sender(async_db, receiver) -> Participant:
    participant, _ = await channels_service.join_channel(async_db, receiver.channel.slug)
    return participant


async def channel_of(async_db, participant: Participant) -> Channel:
    # Явно: связь participant.channel не догружается сама (lazy="raise"), а после refresh
    # в сервисе её в объекте уже нет
    channel = await async_db.get(Channel, participant.channel_id)
    assert channel is not None
    return channel


async def set_last_activity(async_db, channel: Channel, seconds_ago: int) -> None:
    await async_db.execute(
        update(Channel)
        .where(Channel.id == channel.id)
        .values(last_activity_at=func.now() - timedelta(seconds=seconds_ago))
    )


# --- create / join ---


async def test_create_channel(async_db):
    participant, token = await channels_service.create_channel(async_db)

    assert participant.role == ParticipantRole.RECEIVER
    assert participant.channel.slug
    # В базе только хэш, сам токен не хранится
    assert participant.token_hash == hash_token(token)
    assert token not in (await async_db.scalars(select(Participant.token_hash))).all()


async def test_join_channel(async_db, receiver):
    participant, token = await channels_service.join_channel(async_db, receiver.channel.slug)

    assert participant.role == ParticipantRole.SENDER
    assert participant.channel_id == receiver.channel_id
    assert token


async def test_join_after_creator_became_sender(async_db, receiver):
    await channels_service.become_sender(async_db, receiver)

    participant, _ = await channels_service.join_channel(
        async_db, (await channel_of(async_db, receiver)).slug
    )

    assert receiver.role == ParticipantRole.SENDER
    assert participant.role == ParticipantRole.RECEIVER


async def test_join_full_channel(async_db, sender):
    with pytest.raises(channels_service.ChannelFullError):
        await channels_service.join_channel(async_db, sender.channel.slug)


async def test_join_unknown_channel(async_db):
    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.join_channel(async_db, "zzzzzzzz")


async def test_join_closed_channel(async_db, receiver):
    await channels_service.close_channel(async_db, receiver.channel)

    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.join_channel(async_db, receiver.channel.slug)


# --- активность канала ---


async def test_get_active_channel(async_db, receiver):
    assert (
        await channels_service.get_active_channel(async_db, receiver.channel.slug)
        is receiver.channel
    )


async def test_closed_channel_is_not_active(async_db, receiver):
    await channels_service.close_channel(async_db, receiver.channel)

    assert await channels_service.get_active_channel(async_db, receiver.channel.slug) is None


async def test_idle_channel_is_not_active(async_db, receiver):
    await set_last_activity(async_db, receiver.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    assert await channels_service.get_active_channel(async_db, receiver.channel.slug) is None


# --- токены ---


async def test_get_participant_by_token(async_db):
    participant, token = await channels_service.create_channel(async_db)

    assert (
        await channels_service.get_participant(async_db, participant.channel, token) is participant
    )


async def test_get_participant_wrong_token(async_db, receiver):
    assert await channels_service.get_participant(async_db, receiver.channel, "wrong") is None


async def test_get_participant_token_from_other_channel(async_db, receiver):
    _, other_token = await channels_service.create_channel(async_db)

    assert await channels_service.get_participant(async_db, receiver.channel, other_token) is None


# --- сообщения ---


async def test_add_message(async_db, sender):
    message = await channels_service.add_message(async_db, sender, "hello")

    assert message.text == "hello"
    assert message.sender_id == sender.id
    assert message.expires_at - message.created_at == timedelta(
        seconds=settings.MESSAGE_TTL_SECONDS
    )


async def test_add_message_by_receiver_not_allowed(async_db, receiver, sender):
    with pytest.raises(channels_service.ActionNotAllowedError):
        await channels_service.add_message(async_db, receiver, "hello")

    assert (await async_db.scalars(select(Message))).all() == []


async def test_add_message_updates_activity(async_db, sender):
    channel = await channel_of(async_db, sender)
    await set_last_activity(async_db, channel, 60)

    await channels_service.add_message(async_db, sender, "hello")

    await async_db.refresh(channel)
    assert channel.last_activity_at == await async_db.scalar(select(func.now()))


async def test_get_live_messages_in_order(async_db, sender):
    first = await channels_service.add_message(async_db, sender, "first")
    second = await channels_service.add_message(async_db, sender, "second")

    assert await channels_service.get_live_messages(
        async_db, await channel_of(async_db, sender)
    ) == [first, second]


async def test_get_live_messages_skips_expired(async_db, sender):
    expired = await channels_service.add_message(async_db, sender, "old")
    alive = await channels_service.add_message(async_db, sender, "new")
    await async_db.execute(
        update(Message)
        .where(Message.id == expired.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert await channels_service.get_live_messages(
        async_db, await channel_of(async_db, sender)
    ) == [alive]


# --- роли ---


async def test_become_sender_swaps_roles(async_db, receiver, sender):
    await channels_service.become_sender(async_db, receiver)

    assert receiver.role == ParticipantRole.SENDER
    assert sender.role == ParticipantRole.RECEIVER


async def test_become_sender_by_sender_not_allowed(async_db, receiver, sender):
    with pytest.raises(channels_service.ActionNotAllowedError):
        await channels_service.become_sender(async_db, sender)

    assert (receiver.role, sender.role) == (ParticipantRole.RECEIVER, ParticipantRole.SENDER)


async def test_new_sender_can_send_and_old_cannot(async_db, receiver, sender):
    await channels_service.become_sender(async_db, receiver)

    await channels_service.add_message(async_db, receiver, "from new sender")
    with pytest.raises(channels_service.ActionNotAllowedError):
        await channels_service.add_message(async_db, sender, "from old sender")


# --- действия в закрытом канале ---


@pytest.mark.parametrize("close", ["closed", "idle"])
async def test_actions_in_inactive_channel_not_allowed(async_db, receiver, sender, close):
    if close == "closed":
        await channels_service.close_channel(async_db, sender.channel)
    else:
        await set_last_activity(async_db, sender.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.add_message(async_db, sender, "late")
    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.become_sender(async_db, receiver)


# --- close_if_idle ---


async def test_close_if_idle_keeps_active_channel(async_db, receiver):
    await set_last_activity(async_db, receiver.channel, 10)

    seconds_left = await channels_service.close_if_idle(async_db, receiver.channel_id)

    assert seconds_left == pytest.approx(settings.CHANNEL_IDLE_TIMEOUT_SECONDS - 10, abs=1)
    await async_db.refresh(receiver.channel)
    assert receiver.channel.closed_at is None


async def test_close_if_idle_closes_idle_channel(async_db, receiver):
    await set_last_activity(async_db, receiver.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    assert await channels_service.close_if_idle(async_db, receiver.channel_id) is None
    await async_db.refresh(receiver.channel)
    assert receiver.channel.closed_at is not None


async def test_close_if_idle_on_closed_channel(async_db, receiver):
    await channels_service.close_channel(async_db, receiver.channel)

    assert await channels_service.close_if_idle(async_db, receiver.channel_id) is None


# --- чистка ---


async def test_delete_expired_messages(async_db, sender):
    expired = await channels_service.add_message(async_db, sender, "old")
    live = await channels_service.add_message(async_db, sender, "new")
    await async_db.execute(
        update(Message)
        .where(Message.id == expired.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert await channels_service.delete_expired_messages(async_db) == 1
    assert (await async_db.scalars(select(Message.id))).all() == [live.id]


async def test_delete_inactive_channels(async_db, sender):
    await channels_service.add_message(async_db, sender, "text")
    idle, _ = await channels_service.create_channel(async_db)
    closed, _ = await channels_service.create_channel(async_db)
    await set_last_activity(async_db, idle.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)
    await channels_service.close_channel(async_db, closed.channel)
    await set_last_activity(
        async_db, await channel_of(async_db, sender), settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1
    )

    assert await channels_service.delete_inactive_channels(async_db) == 3
    assert (await async_db.scalars(select(Channel.id))).all() == []
    # Каскад: участники и сообщения ушли вместе с каналами
    assert (await async_db.scalars(select(Participant.id))).all() == []
    assert (await async_db.scalars(select(Message.id))).all() == []


async def test_delete_inactive_channels_keeps_active(async_db, sender):
    await set_last_activity(async_db, sender.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS - 10)

    assert await channels_service.delete_inactive_channels(async_db) == 0
    assert (await async_db.scalars(select(func.count()).select_from(Participant))).one() == 2
