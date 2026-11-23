from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.channels.presenters import live_messages_read
from app.api.files.routes import RAW_FILE_BODY, receive_upload
from app.db.connection import DbSession, run_db
from app.db.models.channel import Channel, ParticipantRole
from app.db.models.file import FileOrigin
from app.realtime.manager import ChannelManager
from app.schemas.channel import ChannelCreated, ChannelJoin, ChannelJoined, ChannelState
from app.schemas.channel_events import PeerJoinedEvent
from app.schemas.file import FileRead
from app.services import channels as channels_service
from app.utils.qr import make_qr_svg

router = APIRouter(prefix="/api/channels", tags=["channels"])

ParticipantToken = Annotated[str | None, Header(alias="X-Participant-Token")]


async def _active_channel(db: AsyncSession, slug: str) -> Channel:
    channel = await channels_service.get_active_channel(db, slug)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return channel


@router.post("", response_model=ChannelCreated, status_code=status.HTTP_201_CREATED)
async def create_channel(db: DbSession) -> ChannelCreated:
    participant, token = await channels_service.create_channel(db)
    # Канал присвоен участнику при создании — он уже в объекте, запроса здесь нет
    return ChannelCreated(slug=participant.channel.slug, token=token, role=participant.role)


@router.post(
    "/{slug}/join",
    response_model=ChannelJoined,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "Channel not found"}, 409: {"description": "Channel is full"}},
    description="key_commitment — SHA-256 открытого ключа устройства. Сам ключ уходит по "
    "WebSocket событием public_key, когда придёт ключ создателя.",
)
async def join_channel(slug: str, body: ChannelJoin, request: Request) -> ChannelJoined:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    try:
        participant, token = await run_db(
            factory, lambda db: channels_service.join_channel(db, slug, body.key_commitment)
        )
    except channels_service.ChannelNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Channel not found") from None
    except channels_service.ChannelFullError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Channel is full") from None
    # После коммита: иначе создатель мог бы ответить ключом раньше, чем сервер увидит,
    # что второй участник вошёл
    manager: ChannelManager = request.app.state.channel_manager
    await manager.broadcast(slug, PeerJoinedEvent(key_commitment=body.key_commitment))
    return ChannelJoined(slug=slug, token=token, role=participant.role)


@router.get(
    "/{slug}",
    response_model=ChannelState,
    responses={401: {"description": "Missing or invalid participant token"}},
)
async def get_channel_state(
    slug: str, db: DbSession, token: ParticipantToken = None
) -> ChannelState:
    channel = await _active_channel(db, slug)
    participant = await channels_service.get_participant(db, channel, token) if token else None
    if participant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid participant token")
    participants = await channels_service.count_participants(db, channel)
    keys = await channels_service.get_key_exchange(db, participant)
    return ChannelState(
        slug=channel.slug,
        role=participant.role,
        peer_joined=participants == channels_service.MAX_PARTICIPANTS,
        messages=await live_messages_read(db, channel, viewer_id=participant.id),
        key_sent=keys.key_sent,
        peer_key_commitment=keys.peer_key_commitment,
        peer_public_key=keys.peer_public_key,
    )


@router.get(
    "/{slug}/qr",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}, "description": "QR-код со ссылкой"}},
)
async def channel_qr(slug: str, request: Request, db: DbSession) -> Response:
    channel = await _active_channel(db, slug)
    url = str(request.url_for("channel_page", slug=channel.slug))
    return Response(await run_in_threadpool(make_qr_svg, url), media_type="image/svg+xml")


async def _sender_id(db: AsyncSession, slug: str, token: str | None) -> int:
    channel = await _active_channel(db, slug)
    participant = await channels_service.get_participant(db, channel, token) if token else None
    if participant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid participant token")
    if participant.role != ParticipantRole.SENDER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only the sender can upload files")
    if not await channels_service.keys_exchanged(db, channel.id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Keys are not exchanged yet")
    return participant.id


@router.post(
    "/{slug}/files",
    response_model=FileRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "Missing or invalid participant token"},
        403: {"description": "Only the sender can upload files"},
        404: {"description": "Channel not found"},
        409: {"description": "Keys are not exchanged yet"},
        413: {"description": "File is too large"},
        429: {"description": "Too many uploads"},
        507: {"description": "Storage is full"},
    },
    openapi_extra=RAW_FILE_BODY,
    description="Загрузка файла в канал. В ленту он попадёт событием send_file по WebSocket.",
)
async def upload_channel_file(
    slug: str,
    request: Request,
    name: Annotated[str, Query(min_length=1, max_length=1024)],
    token: ParticipantToken = None,
) -> FileRead:
    # Проверяем до чтения тела: не отправителю незачем принимать мегабайты.
    # Окончательная проверка роли — в send_file, под блокировкой канала
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    sender_id = await run_db(factory, lambda db: _sender_id(db, slug, token))
    return await receive_upload(request, name, origin=FileOrigin.CHANNEL, uploaded_by_id=sender_id)
