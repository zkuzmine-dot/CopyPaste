from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models.channel import Channel, Message, Participant, ParticipantRole
from app.db.models.file import FileOrigin
from app.services import channels as channels_service
from app.services import files as files_service
from app.settings import settings
from app.storage.base import new_storage_key
from app.utils.tokens import hash_token
from tests.channel_keys import DeviceKeys, device_keys, fake_payload, join_ready

pytestmark = pytest.mark.anyio


@pytest.fixture
async def receiver(db) -> Participant:
    participant, _ = await channels_service.create_channel(db)
    return participant


@pytest.fixture
async def sender(db, receiver) -> Participant:
    participant, _ = await join_ready(db, receiver.channel.slug)
    return participant


@pytest.fixture
def joiner_keys() -> DeviceKeys:
    return device_keys()


@pytest.fixture
async def joiner(db, receiver, joiner_keys) -> Participant:
    """Второе устройство вошло, но ключами ещё не обменялись."""
    participant, _ = await channels_service.join_channel(
        db, receiver.channel.slug, joiner_keys.commitment
    )
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


async def test_join_channel(db, receiver, joiner_keys):
    participant, token = await channels_service.join_channel(
        db, receiver.channel.slug, joiner_keys.commitment
    )

    assert participant.role == ParticipantRole.SENDER
    assert participant.channel_id == receiver.channel_id
    assert participant.key_commitment == joiner_keys.commitment
    assert participant.public_key is None
    assert token


async def test_join_after_creator_became_sender(db, receiver):
    await channels_service.become_sender(db, receiver)

    participant, _ = await channels_service.join_channel(
        db, (await channel_of(db, receiver)).slug, device_keys().commitment
    )

    assert receiver.role == ParticipantRole.SENDER
    assert participant.role == ParticipantRole.RECEIVER


async def test_join_full_channel(db, sender):
    with pytest.raises(channels_service.ChannelFullError):
        await channels_service.join_channel(
            db, (await channel_of(db, sender)).slug, device_keys().commitment
        )


async def test_join_unknown_channel(db):
    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.join_channel(db, "zzzzzzzz", device_keys().commitment)


async def test_join_closed_channel(db, receiver):
    await channels_service.close_channel(db, receiver.channel)

    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.join_channel(db, receiver.channel.slug, device_keys().commitment)


# --- обмен ключами ---


async def test_key_exchange_in_order(db, receiver, joiner, joiner_keys):
    creator_key = device_keys().public

    assert await channels_service.submit_public_key(db, receiver, creator_key) == joiner.id
    assert await channels_service.submit_public_key(db, joiner, joiner_keys.public) == receiver.id

    assert (receiver.public_key, joiner.public_key) == (creator_key, joiner_keys.public)
    assert await channels_service.keys_exchanged(db, receiver.channel_id)


async def test_creator_key_before_anyone_joined_is_out_of_order(db, receiver):
    # Ключ создателя до отпечатка второго дал бы серверу время подогнать подменный ключ
    with pytest.raises(channels_service.KeyOutOfOrderError):
        await channels_service.submit_public_key(db, receiver, device_keys().public)

    assert receiver.public_key is None


async def test_joiner_key_before_creator_key_is_out_of_order(db, receiver, joiner, joiner_keys):
    with pytest.raises(channels_service.KeyOutOfOrderError):
        await channels_service.submit_public_key(db, joiner, joiner_keys.public)

    assert joiner.public_key is None


async def test_joiner_key_must_match_commitment(db, receiver, joiner):
    await channels_service.submit_public_key(db, receiver, device_keys().public)

    with pytest.raises(channels_service.KeyMismatchError):
        await channels_service.submit_public_key(db, joiner, device_keys().public)

    assert joiner.public_key is None
    assert not await channels_service.keys_exchanged(db, joiner.channel_id)


async def test_key_is_sent_once(db, receiver, joiner, joiner_keys):
    first = device_keys().public
    await channels_service.submit_public_key(db, receiver, first)
    await channels_service.submit_public_key(db, joiner, joiner_keys.public)

    with pytest.raises(channels_service.KeyAlreadySentError):
        await channels_service.submit_public_key(db, receiver, device_keys().public)
    with pytest.raises(channels_service.KeyAlreadySentError):
        await channels_service.submit_public_key(db, joiner, joiner_keys.public)

    assert receiver.public_key == first


async def test_key_exchange_step_by_step(db, receiver, joiner, joiner_keys):
    creator_key = device_keys().public
    exchange = channels_service.KeyExchange

    assert await channels_service.get_key_exchange(db, receiver) == exchange(
        False, joiner_keys.commitment, None
    )
    assert await channels_service.get_key_exchange(db, joiner) == exchange(False, None, None)

    await channels_service.submit_public_key(db, receiver, creator_key)
    assert await channels_service.get_key_exchange(db, receiver) == exchange(
        True, joiner_keys.commitment, None
    )
    assert await channels_service.get_key_exchange(db, joiner) == exchange(False, None, creator_key)

    await channels_service.submit_public_key(db, joiner, joiner_keys.public)
    assert await channels_service.get_key_exchange(db, receiver) == exchange(
        True, joiner_keys.commitment, joiner_keys.public
    )
    assert await channels_service.get_key_exchange(db, joiner) == exchange(True, None, creator_key)


async def test_key_exchange_updates_activity(db, receiver, joiner):
    channel = await channel_of(db, receiver)
    await set_last_activity(db, channel, 60)

    await channels_service.submit_public_key(db, receiver, device_keys().public)

    await db.refresh(channel)
    assert channel.last_activity_at == await db.scalar(select(func.now()))


async def test_key_in_inactive_channel_not_allowed(db, receiver, joiner):
    await channels_service.close_channel(db, receiver.channel)

    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.submit_public_key(db, receiver, device_keys().public)


async def test_send_before_keys_exchanged_not_allowed(db, joiner):
    with pytest.raises(channels_service.KeysNotReadyError):
        await channels_service.add_message(db, joiner, fake_payload("too early"))

    assert (await db.scalars(select(Message))).all() == []


async def test_send_file_before_keys_exchanged_not_allowed(db, joiner):
    stored = await files_service.create_upload(
        db,
        storage_key=new_storage_key(),
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=1,
        origin=FileOrigin.CHANNEL,
        uploaded_by_id=joiner.id,
    )

    with pytest.raises(channels_service.KeysNotReadyError):
        await channels_service.add_file_message(db, joiner, stored.public_id)

    await db.refresh(stored)
    assert stored.message_id is None
    assert (await db.scalars(select(Message))).all() == []


async def test_send_after_one_key_not_allowed(db, receiver, joiner):
    await channels_service.submit_public_key(db, receiver, device_keys().public)

    with pytest.raises(channels_service.KeysNotReadyError):
        await channels_service.add_message(db, joiner, fake_payload("too early"))


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
    message = await channels_service.add_message(db, sender, fake_payload("hello"))

    assert message.payload == fake_payload("hello")
    assert message.sender_id == sender.id
    assert message.expires_at - message.created_at == timedelta(
        seconds=settings.MESSAGE_TTL_SECONDS
    )


async def test_add_message_by_receiver_not_allowed(db, receiver, sender):
    with pytest.raises(channels_service.ActionNotAllowedError):
        await channels_service.add_message(db, receiver, fake_payload("hello"))

    assert (await db.scalars(select(Message))).all() == []


async def test_add_message_updates_activity(db, sender):
    channel = await channel_of(db, sender)
    await set_last_activity(db, channel, 60)

    await channels_service.add_message(db, sender, fake_payload("hello"))

    await db.refresh(channel)
    assert channel.last_activity_at == await db.scalar(select(func.now()))


async def test_get_live_messages_in_order(db, sender):
    first = await channels_service.add_message(db, sender, fake_payload("first"))
    second = await channels_service.add_message(db, sender, fake_payload("second"))
    channel = await channel_of(db, sender)

    assert await channels_service.get_live_messages(db, channel) == [first, second]


async def test_get_live_messages_skips_expired(db, sender):
    expired = await channels_service.add_message(db, sender, fake_payload("old"))
    alive = await channels_service.add_message(db, sender, fake_payload("new"))
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

    await channels_service.add_message(db, receiver, fake_payload("from new sender"))
    with pytest.raises(channels_service.ActionNotAllowedError):
        await channels_service.add_message(db, sender, fake_payload("from old sender"))


# --- действия в закрытом канале ---


@pytest.mark.parametrize("close", ["closed", "idle"])
async def test_actions_in_inactive_channel_not_allowed(db, receiver, sender, close):
    channel = await channel_of(db, sender)
    if close == "closed":
        await channels_service.close_channel(db, channel)
    else:
        await set_last_activity(db, channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    with pytest.raises(channels_service.ChannelNotFoundError):
        await channels_service.add_message(db, sender, fake_payload("late"))
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
    expired = await channels_service.add_message(db, sender, fake_payload("old"))
    live = await channels_service.add_message(db, sender, fake_payload("new"))
    await db.execute(
        update(Message)
        .where(Message.id == expired.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert await channels_service.delete_expired_messages(db) == 1
    assert (await db.scalars(select(Message.id))).all() == [live.id]


async def test_delete_inactive_channels(db, sender):
    await channels_service.add_message(db, sender, fake_payload("text"))
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
    await set_last_activity(
        db, await channel_of(db, sender), settings.CHANNEL_IDLE_TIMEOUT_SECONDS - 10
    )

    assert await channels_service.delete_inactive_channels(db) == 0
    assert (await db.scalars(select(func.count()).select_from(Participant))).one() == 2
