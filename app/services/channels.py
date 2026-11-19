from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, delete, func, select, update
from sqlalchemy.orm import Session

from app.db.models.channel import Channel, Message, MessageKind, Participant, ParticipantRole
from app.db.models.file import StoredFile
from app.services import files as files_service
from app.settings import settings
from app.utils.tokens import generate_token, hash_token

MAX_PARTICIPANTS = 2


class ChannelNotFoundError(Exception):
    pass


class ChannelFullError(Exception):
    pass


class ActionNotAllowedError(Exception):
    pass


def _is_active() -> ColumnElement[bool]:
    # Без last_activity_at канал, брошенный во время перезапуска app, жил бы до чистки
    idle = timedelta(seconds=settings.CHANNEL_IDLE_TIMEOUT_SECONDS)
    return and_(Channel.closed_at.is_(None), Channel.last_activity_at > func.now() - idle)


def _add_participant(
    db: Session, channel: Channel, role: ParticipantRole
) -> tuple[Participant, str]:
    token = generate_token()
    participant = Participant(channel=channel, token_hash=hash_token(token), role=role)
    db.add(participant)
    db.flush()
    return participant, token


def _lock_active_channel(db: Session, channel_id: int) -> None:
    # Сериализует действия в канале: роль, проверенная после блокировки, не устареет до коммита
    stmt = select(Channel.id).where(Channel.id == channel_id, _is_active()).with_for_update()
    if db.execute(stmt).first() is None:
        raise ChannelNotFoundError


def _touch(db: Session, channel_id: int) -> None:
    db.execute(update(Channel).where(Channel.id == channel_id).values(last_activity_at=func.now()))


def create_channel(db: Session) -> tuple[Participant, str]:
    channel = Channel()
    db.add(channel)
    return _add_participant(db, channel, ParticipantRole.RECEIVER)


def get_active_channel(db: Session, slug: str) -> Channel | None:
    stmt = select(Channel).where(Channel.slug == slug, _is_active())
    return db.execute(stmt).scalar_one_or_none()


def join_channel(db: Session, slug: str) -> tuple[Participant, str]:
    # FOR UPDATE: два одновременных join не пройдут оба проверку на свободный слот
    stmt = select(Channel).where(Channel.slug == slug, _is_active()).with_for_update()
    channel = db.execute(stmt).scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError
    if count_participants(db, channel) >= MAX_PARTICIPANTS:
        raise ChannelFullError
    # Создатель мог стать отправителем, пока был в канале один
    taken = db.scalars(select(Participant.role).where(Participant.channel_id == channel.id)).all()
    role = ParticipantRole.RECEIVER if ParticipantRole.SENDER in taken else ParticipantRole.SENDER
    return _add_participant(db, channel, role)


def count_participants(db: Session, channel: Channel) -> int:
    stmt = select(func.count()).select_from(Participant).where(Participant.channel_id == channel.id)
    return db.execute(stmt).scalar_one()


def get_participant(db: Session, channel: Channel, token: str) -> Participant | None:
    stmt = select(Participant).where(
        Participant.channel_id == channel.id, Participant.token_hash == hash_token(token)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_live_messages(db: Session, channel: Channel) -> Sequence[Message]:
    stmt = (
        select(Message)
        .where(Message.channel_id == channel.id, Message.expires_at > func.now())
        .order_by(Message.created_at, Message.id)
    )
    return db.execute(stmt).scalars().all()


def _lock_as_sender(db: Session, participant: Participant) -> None:
    _lock_active_channel(db, participant.channel_id)
    db.refresh(participant)
    if participant.role != ParticipantRole.SENDER:
        raise ActionNotAllowedError


def add_message(db: Session, participant: Participant, text: str) -> Message:
    _lock_as_sender(db, participant)
    message = Message(
        channel_id=participant.channel_id,
        sender_id=participant.id,
        text=text,
        expires_at=func.now() + timedelta(seconds=settings.MESSAGE_TTL_SECONDS),
    )
    db.add(message)
    _touch(db, participant.channel_id)
    db.flush()
    return message


def add_file_message(
    db: Session, participant: Participant, public_id: str
) -> tuple[Message, StoredFile]:
    """FilesNotAvailableError, если файл не свой или уже отправлен: транзакцию нужно откатить."""
    _lock_as_sender(db, participant)
    message = Message(
        channel_id=participant.channel_id,
        sender_id=participant.id,
        kind=MessageKind.FILE,
        text=None,
        # Дольше текста: большой файл по медленной сети успевает скачаться
        expires_at=func.now() + timedelta(seconds=settings.CHANNEL_FILE_TTL_SECONDS),
    )
    db.add(message)
    db.flush()
    stored = files_service.attach_to_message(
        db, message.id, public_id, uploaded_by_id=participant.id
    )
    _touch(db, participant.channel_id)
    db.flush()
    return message, stored


def become_sender(db: Session, participant: Participant) -> None:
    _lock_active_channel(db, participant.channel_id)
    db.refresh(participant)
    if participant.role != ParticipantRole.RECEIVER:
        raise ActionNotAllowedError
    stmt = select(Participant).where(Participant.channel_id == participant.channel_id)
    for member in db.execute(stmt).scalars():
        is_new_sender = member.id == participant.id
        member.role = ParticipantRole.SENDER if is_new_sender else ParticipantRole.RECEIVER
    _touch(db, participant.channel_id)
    db.flush()


def close_channel(db: Session, channel: Channel) -> None:
    channel.closed_at = func.now()
    db.flush()


def close_if_idle(db: Session, channel_id: int) -> float | None:
    """Закрывает канал, если он бездействует. Иначе — сколько секунд осталось до закрытия."""
    idle = timedelta(seconds=settings.CHANNEL_IDLE_TIMEOUT_SECONDS)
    db.execute(
        update(Channel)
        .where(
            Channel.id == channel_id,
            Channel.closed_at.is_(None),
            Channel.last_activity_at <= func.now() - idle,
        )
        .values(closed_at=func.now())
    )
    seconds_left = db.scalar(
        select(func.extract("epoch", Channel.last_activity_at + idle - func.now())).where(
            Channel.id == channel_id, _is_active()
        )
    )
    return None if seconds_left is None else float(seconds_left)


def delete_expired_messages(db: Session) -> int:
    stmt = (
        delete(Message)
        .where(Message.expires_at <= func.now())
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult[Any], db.execute(stmt)).rowcount


def delete_inactive_channels(db: Session) -> int:
    # Закрытые и бездействующие сразу: в них уже ничего нельзя сделать, а таймер в app
    # мог потеряться при перезапуске. Участники и сообщения удаляются каскадом
    stmt = delete(Channel).where(~_is_active()).execution_options(synchronize_session=False)
    return cast(CursorResult[Any], db.execute(stmt)).rowcount
