from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import Channel, Message, MessageKind, Participant, ParticipantRole
from app.db.models.file import StoredFile
from app.services import files as files_service
from app.settings import settings
from app.utils import channel_keys
from app.utils.tokens import generate_token, hash_token

MAX_PARTICIPANTS = 2


class ChannelNotFoundError(Exception):
    pass


class ChannelFullError(Exception):
    pass


class ActionNotAllowedError(Exception):
    pass


class KeysNotReadyError(Exception):
    pass


class KeyExchangeError(Exception):
    pass


class KeyOutOfOrderError(KeyExchangeError):
    pass


class KeyAlreadySentError(KeyExchangeError):
    pass


class KeyMismatchError(KeyExchangeError):
    pass


@dataclass(frozen=True)
class KeyExchange:
    key_sent: bool
    peer_key_commitment: bytes | None
    peer_public_key: bytes | None


def _is_active() -> ColumnElement[bool]:
    # Без last_activity_at канал, брошенный во время перезапуска app, жил бы до чистки
    idle = timedelta(seconds=settings.CHANNEL_IDLE_TIMEOUT_SECONDS)
    return and_(Channel.closed_at.is_(None), Channel.last_activity_at > func.now() - idle)


async def _add_participant(
    db: AsyncSession, channel: Channel, role: ParticipantRole, key_commitment: bytes | None = None
) -> tuple[Participant, str]:
    token = generate_token()
    participant = Participant(
        channel=channel, token_hash=hash_token(token), role=role, key_commitment=key_commitment
    )
    db.add(participant)
    await db.flush()
    return participant, token


async def _lock_active_channel(db: AsyncSession, channel_id: int) -> None:
    # Сериализует действия в канале: роль, проверенная после блокировки, не устареет до коммита
    stmt = select(Channel.id).where(Channel.id == channel_id, _is_active()).with_for_update()
    if (await db.execute(stmt)).first() is None:
        raise ChannelNotFoundError


async def _touch(db: AsyncSession, channel_id: int) -> None:
    await db.execute(
        update(Channel).where(Channel.id == channel_id).values(last_activity_at=func.now())
    )


async def create_channel(db: AsyncSession) -> tuple[Participant, str]:
    channel = Channel()
    db.add(channel)
    return await _add_participant(db, channel, ParticipantRole.RECEIVER)


async def get_active_channel(db: AsyncSession, slug: str) -> Channel | None:
    stmt = select(Channel).where(Channel.slug == slug, _is_active())
    return (await db.execute(stmt)).scalar_one_or_none()


async def join_channel(
    db: AsyncSession, slug: str, key_commitment: bytes
) -> tuple[Participant, str]:
    # FOR UPDATE: два одновременных join не пройдут оба проверку на свободный слот
    stmt = select(Channel).where(Channel.slug == slug, _is_active()).with_for_update()
    channel = (await db.execute(stmt)).scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError
    if await count_participants(db, channel) >= MAX_PARTICIPANTS:
        raise ChannelFullError
    # Создатель мог стать отправителем, пока был в канале один
    roles = select(Participant.role).where(Participant.channel_id == channel.id)
    taken = (await db.scalars(roles)).all()
    role = ParticipantRole.RECEIVER if ParticipantRole.SENDER in taken else ParticipantRole.SENDER
    return await _add_participant(db, channel, role, key_commitment)


async def count_participants(db: AsyncSession, channel: Channel) -> int:
    stmt = select(func.count()).select_from(Participant).where(Participant.channel_id == channel.id)
    return (await db.execute(stmt)).scalar_one()


async def get_participant(db: AsyncSession, channel: Channel, token: str) -> Participant | None:
    stmt = select(Participant).where(
        Participant.channel_id == channel.id, Participant.token_hash == hash_token(token)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_live_messages(db: AsyncSession, channel: Channel) -> Sequence[Message]:
    stmt = (
        select(Message)
        .where(Message.channel_id == channel.id, Message.expires_at > func.now())
        .order_by(Message.created_at, Message.id)
    )
    return (await db.execute(stmt)).scalars().all()


async def _peer(db: AsyncSession, participant: Participant) -> Participant | None:
    stmt = select(Participant).where(
        Participant.channel_id == participant.channel_id, Participant.id != participant.id
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_key_exchange(db: AsyncSession, participant: Participant) -> KeyExchange:
    peer = await _peer(db, participant)
    return KeyExchange(
        key_sent=participant.public_key is not None,
        peer_key_commitment=peer.key_commitment if peer else None,
        peer_public_key=peer.public_key if peer else None,
    )


async def keys_exchanged(db: AsyncSession, channel_id: int) -> bool:
    stmt = (
        select(func.count())
        .select_from(Participant)
        .where(Participant.channel_id == channel_id, Participant.public_key.is_not(None))
    )
    return (await db.execute(stmt)).scalar_one() == MAX_PARTICIPANTS


async def submit_public_key(db: AsyncSession, participant: Participant, public_key: bytes) -> int:
    """Сохраняет открытый ключ участника. Возвращает id собеседника — ему ключ нужно переслать.

    Создатель отправляет ключ только после отпечатка подключившегося, подключившийся — только
    после ключа создателя и только с тем же отпечатком. Каждый — один раз.
    """
    await _lock_active_channel(db, participant.channel_id)
    await db.refresh(participant)
    if participant.public_key is not None:
        raise KeyAlreadySentError
    # Без отпечатка не войти, поэтому создателю достаточно, что второй уже в канале
    peer = await _peer(db, participant)
    if peer is None:
        raise KeyOutOfOrderError
    commitment = participant.key_commitment
    if commitment is not None:
        if peer.public_key is None:
            raise KeyOutOfOrderError
        if not channel_keys.matches_commitment(public_key, commitment):
            raise KeyMismatchError
    participant.public_key = public_key
    await _touch(db, participant.channel_id)
    await db.flush()
    return peer.id


async def _lock_as_sender(db: AsyncSession, participant: Participant) -> None:
    await _lock_active_channel(db, participant.channel_id)
    await db.refresh(participant)
    if participant.role != ParticipantRole.SENDER:
        raise ActionNotAllowedError
    if not await keys_exchanged(db, participant.channel_id):
        raise KeysNotReadyError


async def _insert_message(
    db: AsyncSession, participant: Participant, kind: MessageKind, text: str | None, ttl: int
) -> Message:
    # INSERT ... RETURNING, а не db.add: срок считает БД (её now()), и он сразу приходит в
    # объект. После обычного flush атрибут с SQL-выражением «протухает» и догружается
    # скрытым SELECT при чтении — в async так нельзя
    stmt = (
        insert(Message)
        .values(
            channel_id=participant.channel_id,
            sender_id=participant.id,
            kind=kind,
            text=text,
            expires_at=func.now() + timedelta(seconds=ttl),
        )
        .returning(Message)
    )
    return (await db.scalars(stmt)).one()


async def add_message(db: AsyncSession, participant: Participant, text: str) -> Message:
    await _lock_as_sender(db, participant)
    message = await _insert_message(
        db, participant, MessageKind.TEXT, text, settings.MESSAGE_TTL_SECONDS
    )
    await _touch(db, participant.channel_id)
    return message


async def add_file_message(
    db: AsyncSession, participant: Participant, public_id: str
) -> tuple[Message, StoredFile]:
    """FilesNotAvailableError, если файл не свой или уже отправлен: транзакцию нужно откатить."""
    await _lock_as_sender(db, participant)
    # Дольше текста: большой файл по медленной сети успевает скачаться
    message = await _insert_message(
        db, participant, MessageKind.FILE, None, settings.CHANNEL_FILE_TTL_SECONDS
    )
    stored = await files_service.attach_to_message(
        db, message.id, public_id, uploaded_by_id=participant.id
    )
    await _touch(db, participant.channel_id)
    return message, stored


async def become_sender(db: AsyncSession, participant: Participant) -> None:
    await _lock_active_channel(db, participant.channel_id)
    await db.refresh(participant)
    if participant.role != ParticipantRole.RECEIVER:
        raise ActionNotAllowedError
    stmt = select(Participant).where(Participant.channel_id == participant.channel_id)
    for member in (await db.execute(stmt)).scalars():
        is_new_sender = member.id == participant.id
        member.role = ParticipantRole.SENDER if is_new_sender else ParticipantRole.RECEIVER
    await _touch(db, participant.channel_id)
    await db.flush()


async def close_channel(db: AsyncSession, channel: Channel) -> None:
    channel.closed_at = func.now()
    await db.flush()


async def close_if_idle(db: AsyncSession, channel_id: int) -> float | None:
    """Закрывает канал, если он бездействует. Иначе — сколько секунд осталось до закрытия."""
    idle = timedelta(seconds=settings.CHANNEL_IDLE_TIMEOUT_SECONDS)
    await db.execute(
        update(Channel)
        .where(
            Channel.id == channel_id,
            Channel.closed_at.is_(None),
            Channel.last_activity_at <= func.now() - idle,
        )
        .values(closed_at=func.now())
    )
    seconds_left = await db.scalar(
        select(func.extract("epoch", Channel.last_activity_at + idle - func.now())).where(
            Channel.id == channel_id, _is_active()
        )
    )
    return None if seconds_left is None else float(seconds_left)


async def delete_expired_messages(db: AsyncSession) -> int:
    stmt = (
        delete(Message)
        .where(Message.expires_at <= func.now())
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult[Any], await db.execute(stmt)).rowcount


async def delete_inactive_channels(db: AsyncSession) -> int:
    # Закрытые и бездействующие сразу: в них уже ничего нельзя сделать, а таймер в app
    # мог потеряться при перезапуске. Участники и сообщения удаляются каскадом
    stmt = delete(Channel).where(~_is_active()).execution_options(synchronize_session=False)
    return cast(CursorResult[Any], await db.execute(stmt)).rowcount
