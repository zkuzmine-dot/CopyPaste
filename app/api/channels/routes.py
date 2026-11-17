from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from app.db.connection import DbSession
from app.db.models.channel import Channel
from app.schemas.channel import ChannelCreated, ChannelJoined, ChannelState, MessageRead
from app.services import channels as channels_service
from app.utils.qr import make_qr_svg

router = APIRouter(prefix="/api/channels", tags=["channels"])

ParticipantToken = Annotated[str | None, Header(alias="X-Participant-Token")]


def get_channel_or_404(db: DbSession, slug: str) -> Channel:
    channel = channels_service.get_active_channel(db, slug)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return channel


@router.post("", response_model=ChannelCreated, status_code=status.HTTP_201_CREATED)
def create_channel(db: DbSession) -> ChannelCreated:
    participant, token = channels_service.create_channel(db)
    return ChannelCreated(slug=participant.channel.slug, token=token, role=participant.role)


@router.post(
    "/{slug}/join",
    response_model=ChannelJoined,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "Channel not found"}, 409: {"description": "Channel is full"}},
)
def join_channel(slug: str, db: DbSession) -> ChannelJoined:
    try:
        participant, token = channels_service.join_channel(db, slug)
    except channels_service.ChannelNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Channel not found") from None
    except channels_service.ChannelFullError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Channel is full") from None
    return ChannelJoined(slug=slug, token=token, role=participant.role)


@router.get(
    "/{slug}",
    response_model=ChannelState,
    responses={401: {"description": "Missing or invalid participant token"}},
)
def get_channel_state(slug: str, db: DbSession, token: ParticipantToken = None) -> ChannelState:
    channel = get_channel_or_404(db, slug)
    participant = channels_service.get_participant(db, channel, token) if token else None
    if participant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid participant token")
    return ChannelState(
        slug=channel.slug,
        role=participant.role,
        peer_joined=channels_service.count_participants(db, channel)
        == channels_service.MAX_PARTICIPANTS,
        messages=[
            MessageRead.model_validate(m) for m in channels_service.get_live_messages(db, channel)
        ],
    )


@router.get(
    "/{slug}/qr",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}, "description": "QR-код со ссылкой"}},
)
def channel_qr(slug: str, request: Request, db: DbSession) -> Response:
    channel = get_channel_or_404(db, slug)
    url = str(request.url_for("channel_page", slug=channel.slug))
    return Response(make_qr_svg(url), media_type="image/svg+xml")
