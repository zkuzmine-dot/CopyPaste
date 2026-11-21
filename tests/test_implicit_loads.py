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


async def test_expired_attribute_is_not_loaded_silently(db):
    note = await flushed_note(db)

    with pytest.raises(ImplicitLoadError):
        _ = note.text


async def test_explicit_refresh_is_allowed(db):
    note = await flushed_note(db)

    await db.refresh(note)

    assert note.text == "hello"


async def test_participant_channel_is_never_lazy_loaded(db):
    created, _ = await channels_service.create_channel(db)
    participant_id = created.id
    db.expunge_all()

    participant = await db.get(Participant, participant_id)
    with pytest.raises(InvalidRequestError, match="lazy='raise'"):
        _ = participant.channel

    db.expunge_all()
    options = [joinedload(Participant.channel)]
    loaded = await db.get(Participant, participant_id, options=options)
    assert loaded.channel.id == loaded.channel_id
