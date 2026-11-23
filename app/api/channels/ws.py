import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.interfaces import ORMOption

from app.api.channels.presenters import live_messages_read, message_read
from app.db.connection import run_db
from app.db.models.channel import Participant, ParticipantRole
from app.realtime.manager import ChannelManager, CloseCode, Connection
from app.realtime.rate_limit import TokenBucket
from app.schemas.channel import MessageRead
from app.schemas.channel_events import (
    AuthEvent,
    BecomeSenderEvent,
    ChannelClosedEvent,
    ClientEvent,
    ErrorEvent,
    MessageEvent,
    PeerKeyEvent,
    PeerStatusEvent,
    PublicKeyEvent,
    RolesChangedEvent,
    SendEvent,
    SendFileEvent,
    StateEvent,
    client_event_adapter,
)
from app.services import channels as channels_service
from app.services import files as files_service
from app.settings import settings

router = APIRouter()


@dataclass(frozen=True)
class Identity:
    channel_id: int
    participant_id: int


class InvalidEventError(Exception):
    pass


class MessageTooBigError(Exception):
    pass


KEYS_NOT_READY = ErrorEvent(code="keys_not_ready", detail="Devices have not exchanged keys yet")
KEY_EXCHANGE_ERRORS: dict[type[channels_service.KeyExchangeError], ErrorEvent] = {
    channels_service.KeyOutOfOrderError: ErrorEvent(
        code="key_out_of_order", detail="Wait for the peer's commitment or key"
    ),
    channels_service.KeyAlreadySentError: ErrorEvent(
        code="key_already_sent", detail="Your key is already sent"
    ),
    channels_service.KeyMismatchError: ErrorEvent(
        code="key_mismatch", detail="The key does not match your commitment"
    ),
}


async def _receive_event(websocket: WebSocket) -> ClientEvent:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text = message.get("text")
    if text is None:
        raise InvalidEventError("Expected a text frame with JSON")
    # Основной лимит — --ws-max-size у uvicorn, он не даст дочитать огромное сообщение.
    # Эта проверка — на случай запуска без него
    if len(text.encode()) > settings.WS_MAX_MESSAGE_BYTES:
        raise MessageTooBigError
    try:
        return client_event_adapter.validate_json(text)
    except ValidationError as exc:
        raise InvalidEventError(exc.errors()[0]["msg"]) from None


async def _identify(db: AsyncSession, slug: str, token: str) -> Identity | CloseCode:
    channel = await channels_service.get_active_channel(db, slug)
    if channel is None:
        return CloseCode.CHANNEL_NOT_FOUND
    participant = await channels_service.get_participant(db, channel, token)
    if participant is None:
        return CloseCode.UNAUTHORIZED
    return Identity(channel_id=channel.id, participant_id=participant.id)


async def _authenticate(
    websocket: WebSocket, factory: async_sessionmaker[AsyncSession], slug: str
) -> Identity | None:
    try:
        async with asyncio.timeout(settings.WS_AUTH_TIMEOUT_SECONDS):
            event = await _receive_event(websocket)
    except (TimeoutError, InvalidEventError):
        event = None
    except MessageTooBigError:
        await websocket.close(CloseCode.MESSAGE_TOO_BIG)
        return None
    if not isinstance(event, AuthEvent):
        await websocket.close(CloseCode.UNAUTHORIZED)
        return None
    token = event.token
    result = await run_db(factory, lambda db: _identify(db, slug, token))
    if isinstance(result, CloseCode):
        await websocket.close(result)
        return None
    return result


@dataclass(frozen=True)
class LoadedState:
    role: ParticipantRole
    peer_joined: bool
    messages: list[MessageRead]
    keys: channels_service.KeyExchange


async def _get_participant(
    db: AsyncSession, participant_id: int, *options: ORMOption
) -> Participant:
    # Чистка удаляет канал вместе с участниками, пока вкладка ещё открыта
    participant = await db.get(Participant, participant_id, options=options)
    if participant is None:
        raise channels_service.ChannelNotFoundError
    return participant


async def _load_state(db: AsyncSession, participant_id: int) -> LoadedState:
    # Канал — тем же запросом: сама связь не догружается (lazy="raise")
    participant = await _get_participant(db, participant_id, joinedload(Participant.channel))
    channel = participant.channel
    participants = await channels_service.count_participants(db, channel)
    return LoadedState(
        role=ParticipantRole(participant.role),
        peer_joined=participants == channels_service.MAX_PARTICIPANTS,
        messages=await live_messages_read(db, channel, viewer_id=participant_id),
        keys=await channels_service.get_key_exchange(db, participant),
    )


async def _add_message(db: AsyncSession, participant_id: int, text: str) -> MessageEvent:
    participant = await _get_participant(db, participant_id)
    message = await channels_service.add_message(db, participant, text)
    return MessageEvent.model_validate(message)


@dataclass(frozen=True)
class FileMessageEvents:
    for_uploader: MessageEvent
    for_others: MessageEvent


async def _add_file_message(
    db: AsyncSession, participant_id: int, file_id: str
) -> FileMessageEvents:
    participant = await _get_participant(db, participant_id)
    message, stored = await channels_service.add_file_message(db, participant, file_id)
    return FileMessageEvents(
        for_uploader=MessageEvent(**message_read(message, stored, with_links=False).model_dump()),
        for_others=MessageEvent(**message_read(message, stored, with_links=True).model_dump()),
    )


async def _become_sender(db: AsyncSession, participant_id: int) -> None:
    await channels_service.become_sender(db, await _get_participant(db, participant_id))


async def _submit_public_key(db: AsyncSession, participant_id: int, key: bytes) -> int:
    participant = await _get_participant(db, participant_id)
    return await channels_service.submit_public_key(db, participant, key)


async def _close_if_idle(
    manager: ChannelManager, factory: async_sessionmaker[AsyncSession], slug: str, channel_id: int
) -> float | None:
    seconds_left = await run_db(factory, lambda db: channels_service.close_if_idle(db, channel_id))
    if seconds_left is None:
        await manager.close_room(slug, ChannelClosedEvent(reason="idle"), CloseCode.CHANNEL_CLOSED)
    return seconds_left


async def _watch_idle(
    manager: ChannelManager, factory: async_sessionmaker[AsyncSession], slug: str, channel_id: int
) -> None:
    # Срок берём из last_activity_at в БД, поэтому таймер не нужно сбрасывать вручную
    while (seconds_left := await _close_if_idle(manager, factory, slug, channel_id)) is not None:
        await asyncio.sleep(seconds_left)


@router.websocket("/ws/channels/{slug}")
async def channel_ws(websocket: WebSocket, slug: str) -> None:
    factory: async_sessionmaker[AsyncSession] = websocket.app.state.session_factory
    manager: ChannelManager = websocket.app.state.channel_manager
    await websocket.accept()
    try:
        identity = await _authenticate(websocket, factory, slug)
    except WebSocketDisconnect:
        return
    if identity is None:
        return

    me = identity.participant_id
    connection = Connection(websocket)
    if not await manager.connect(slug, me, connection):
        await manager.broadcast(slug, PeerStatusEvent(online=True), exclude=me)
    manager.watch(slug, lambda: _watch_idle(manager, factory, slug, identity.channel_id))

    try:
        try:
            loaded = await run_db(factory, lambda db: _load_state(db, me))
        except channels_service.ChannelNotFoundError:
            await _close_if_idle(manager, factory, slug, identity.channel_id)
            return
        state = StateEvent(
            role=loaded.role,
            peer_joined=loaded.peer_joined,
            peer_online=manager.is_peer_online(slug, me),
            messages=loaded.messages,
            key_sent=loaded.keys.key_sent,
            peer_key_commitment=loaded.keys.peer_key_commitment,
            peer_public_key=loaded.keys.peer_public_key,
        )
        await connection.start(state)
        rate_limit = TokenBucket(settings.WS_RATE_LIMIT_PER_SECOND, settings.WS_RATE_LIMIT_BURST)
        while True:
            try:
                event = await _receive_event(websocket)
            except InvalidEventError as exc:
                await connection.send(ErrorEvent(code="invalid_event", detail=str(exc)))
                continue
            except MessageTooBigError:
                await connection.close(CloseCode.MESSAGE_TOO_BIG)
                return
            if not rate_limit.allow():
                error = ErrorEvent(code="rate_limited", detail="Too many events, slow down")
                await connection.send(error)
                continue
            try:
                await _handle_event(event, connection, manager, factory, slug, me)
            except channels_service.ChannelNotFoundError:
                await _close_if_idle(manager, factory, slug, identity.channel_id)
    except WebSocketDisconnect:
        pass
    finally:
        if manager.disconnect(slug, me, connection):
            await manager.broadcast(slug, PeerStatusEvent(online=False))


async def _handle_event(
    event: ClientEvent,
    connection: Connection,
    manager: ChannelManager,
    factory: async_sessionmaker[AsyncSession],
    slug: str,
    me: int,
) -> None:
    match event:
        case SendEvent(text=text):
            try:
                message = await run_db(factory, lambda db: _add_message(db, me, text))
            except channels_service.ActionNotAllowedError:
                error = ErrorEvent(code="not_sender", detail="Only the sender can send messages")
                await connection.send(error)
                return
            except channels_service.KeysNotReadyError:
                await connection.send(KEYS_NOT_READY)
                return
            await manager.broadcast(slug, message)
        case SendFileEvent(file_id=file_id):
            try:
                events = await run_db(factory, lambda db: _add_file_message(db, me, file_id))
            except channels_service.ActionNotAllowedError:
                error = ErrorEvent(code="not_sender", detail="Only the sender can send files")
                await connection.send(error)
                return
            except channels_service.KeysNotReadyError:
                await connection.send(KEYS_NOT_READY)
                return
            except files_service.FilesNotAvailableError:
                error = ErrorEvent(
                    code="file_not_found",
                    detail="File is not found, not yours or already sent",
                )
                await connection.send(error)
                return
            for pid in manager.participant_ids(slug):
                await manager.send_to(
                    slug, pid, events.for_uploader if pid == me else events.for_others
                )
        case BecomeSenderEvent():
            try:
                await run_db(factory, lambda db: _become_sender(db, me))
            except channels_service.ActionNotAllowedError:
                error = ErrorEvent(code="not_receiver", detail="You are already the sender")
                await connection.send(error)
                return
            for pid in manager.participant_ids(slug):
                role = ParticipantRole.SENDER if pid == me else ParticipantRole.RECEIVER
                await manager.send_to(slug, pid, RolesChangedEvent(role=role))
        case PublicKeyEvent(key=key):
            try:
                peer = await run_db(factory, lambda db: _submit_public_key(db, me, key))
            except channels_service.KeyExchangeError as exc:
                await connection.send(KEY_EXCHANGE_ERRORS[type(exc)])
                return
            await manager.send_to(slug, peer, PeerKeyEvent(key=key))
        case AuthEvent():
            error = ErrorEvent(code="already_authenticated", detail="Already authenticated")
            await connection.send(error)
