import pytest
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models.channel import Participant
from app.db.models.note import Note
from app.services import channels as channels_service
from tests.conftest import ImplicitLoadError

pytestmark = pytest.mark.anyio

# Сторож из conftest: скрытая догрузка — это запрос, которого в коде не видно. В async она
# невозможна, и сторож называет её прямо, а не через MissingGreenlet


async def flushed_note(db: AsyncSession) -> Note:
    note = Note(text="hello")
    db.add(note)
    await db.flush()
    db.expire(note, ["text"])
    return note


async def test_expired_attribute_is_not_loaded_silently(async_db):
    note = await flushed_note(async_db)

    with pytest.raises(ImplicitLoadError):
        _ = note.text


async def test_explicit_refresh_is_allowed(async_db):
    note = await flushed_note(async_db)

    await async_db.refresh(note)

    assert note.text == "hello"


async def test_participant_channel_is_never_lazy_loaded(async_db):
    created, _ = await channels_service.create_channel(async_db)
    participant_id = created.id
    async_db.expunge_all()

    participant = await async_db.get(Participant, participant_id)
    with pytest.raises(InvalidRequestError, match="lazy='raise'"):
        _ = participant.channel

    async_db.expunge_all()
    options = [joinedload(Participant.channel)]
    loaded = await async_db.get(Participant, participant_id, options=options)
    assert loaded.channel.id == loaded.channel_id
