from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models.channel import Channel, Message, Participant, ParticipantRole
from app.services import channels as channels_service
from app.settings import settings
from app.utils.tokens import hash_token


@pytest.fixture
def receiver(db) -> Participant:
    participant, _ = channels_service.create_channel(db)
    return participant


@pytest.fixture
def sender(db, receiver) -> Participant:
    participant, _ = channels_service.join_channel(db, receiver.channel.slug)
    return participant


def set_last_activity(db, channel: Channel, seconds_ago: int) -> None:
    db.execute(
        update(Channel)
        .where(Channel.id == channel.id)
        .values(last_activity_at=func.now() - timedelta(seconds=seconds_ago))
    )


# --- create / join ---


def test_create_channel(db):
    participant, token = channels_service.create_channel(db)

    assert participant.role == ParticipantRole.RECEIVER
    assert participant.channel.slug
    # В базе только хэш, сам токен не хранится
    assert participant.token_hash == hash_token(token)
    assert token not in db.scalars(select(Participant.token_hash)).all()


def test_join_channel(db, receiver):
    participant, token = channels_service.join_channel(db, receiver.channel.slug)

    assert participant.role == ParticipantRole.SENDER
    assert participant.channel_id == receiver.channel_id
    assert token


def test_join_after_creator_became_sender(db, receiver):
    channels_service.become_sender(db, receiver)

    participant, _ = channels_service.join_channel(db, receiver.channel.slug)

    assert receiver.role == ParticipantRole.SENDER
    assert participant.role == ParticipantRole.RECEIVER


def test_join_full_channel(db, sender):
    with pytest.raises(channels_service.ChannelFullError):
        channels_service.join_channel(db, sender.channel.slug)


def test_join_unknown_channel(db):
    with pytest.raises(channels_service.ChannelNotFoundError):
        channels_service.join_channel(db, "zzzzzzzz")


def test_join_closed_channel(db, receiver):
    channels_service.close_channel(db, receiver.channel)

    with pytest.raises(channels_service.ChannelNotFoundError):
        channels_service.join_channel(db, receiver.channel.slug)


# --- активность канала ---


def test_get_active_channel(db, receiver):
    assert channels_service.get_active_channel(db, receiver.channel.slug) is receiver.channel


def test_closed_channel_is_not_active(db, receiver):
    channels_service.close_channel(db, receiver.channel)

    assert channels_service.get_active_channel(db, receiver.channel.slug) is None


def test_idle_channel_is_not_active(db, receiver):
    set_last_activity(db, receiver.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    assert channels_service.get_active_channel(db, receiver.channel.slug) is None


# --- токены ---


def test_get_participant_by_token(db):
    participant, token = channels_service.create_channel(db)

    assert channels_service.get_participant(db, participant.channel, token) is participant


def test_get_participant_wrong_token(db, receiver):
    assert channels_service.get_participant(db, receiver.channel, "wrong") is None


def test_get_participant_token_from_other_channel(db, receiver):
    _, other_token = channels_service.create_channel(db)

    assert channels_service.get_participant(db, receiver.channel, other_token) is None


# --- сообщения ---


def test_add_message(db, sender):
    message = channels_service.add_message(db, sender, "hello")

    assert message.text == "hello"
    assert message.sender_id == sender.id
    assert message.expires_at - message.created_at == timedelta(
        seconds=settings.MESSAGE_TTL_SECONDS
    )


def test_add_message_by_receiver_not_allowed(db, receiver, sender):
    with pytest.raises(channels_service.ActionNotAllowedError):
        channels_service.add_message(db, receiver, "hello")

    assert db.scalars(select(Message)).all() == []


def test_add_message_updates_activity(db, sender):
    set_last_activity(db, sender.channel, 60)

    channels_service.add_message(db, sender, "hello")

    db.refresh(sender.channel)
    assert sender.channel.last_activity_at == db.scalar(select(func.now()))


def test_get_live_messages_in_order(db, sender):
    first = channels_service.add_message(db, sender, "first")
    second = channels_service.add_message(db, sender, "second")

    assert channels_service.get_live_messages(db, sender.channel) == [first, second]


def test_get_live_messages_skips_expired(db, sender):
    expired = channels_service.add_message(db, sender, "old")
    alive = channels_service.add_message(db, sender, "new")
    db.execute(
        update(Message)
        .where(Message.id == expired.id)
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert channels_service.get_live_messages(db, sender.channel) == [alive]


# --- роли ---


def test_become_sender_swaps_roles(db, receiver, sender):
    channels_service.become_sender(db, receiver)

    assert receiver.role == ParticipantRole.SENDER
    assert sender.role == ParticipantRole.RECEIVER


def test_become_sender_by_sender_not_allowed(db, receiver, sender):
    with pytest.raises(channels_service.ActionNotAllowedError):
        channels_service.become_sender(db, sender)

    assert (receiver.role, sender.role) == (ParticipantRole.RECEIVER, ParticipantRole.SENDER)


def test_new_sender_can_send_and_old_cannot(db, receiver, sender):
    channels_service.become_sender(db, receiver)

    channels_service.add_message(db, receiver, "from new sender")
    with pytest.raises(channels_service.ActionNotAllowedError):
        channels_service.add_message(db, sender, "from old sender")


# --- действия в закрытом канале ---


@pytest.mark.parametrize("close", ["closed", "idle"])
def test_actions_in_inactive_channel_not_allowed(db, receiver, sender, close):
    if close == "closed":
        channels_service.close_channel(db, sender.channel)
    else:
        set_last_activity(db, sender.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    with pytest.raises(channels_service.ChannelNotFoundError):
        channels_service.add_message(db, sender, "late")
    with pytest.raises(channels_service.ChannelNotFoundError):
        channels_service.become_sender(db, receiver)


# --- close_if_idle ---


def test_close_if_idle_keeps_active_channel(db, receiver):
    set_last_activity(db, receiver.channel, 10)

    seconds_left = channels_service.close_if_idle(db, receiver.channel_id)

    assert seconds_left == pytest.approx(settings.CHANNEL_IDLE_TIMEOUT_SECONDS - 10, abs=1)
    db.refresh(receiver.channel)
    assert receiver.channel.closed_at is None


def test_close_if_idle_closes_idle_channel(db, receiver):
    set_last_activity(db, receiver.channel, settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)

    assert channels_service.close_if_idle(db, receiver.channel_id) is None
    db.refresh(receiver.channel)
    assert receiver.channel.closed_at is not None


def test_close_if_idle_on_closed_channel(db, receiver):
    channels_service.close_channel(db, receiver.channel)

    assert channels_service.close_if_idle(db, receiver.channel_id) is None
