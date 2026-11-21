import pytest
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import joinedload

from app.db.models.channel import Participant
from app.db.models.note import Note
from app.services import channels as channels_service
from tests.conftest import ImplicitLoadError

# Сторож из conftest: в async скрытая догрузка невозможна, поэтому тесты ловят её уже сейчас


def flushed_note(db) -> Note:
    note = Note(text="hello")
    db.add(note)
    db.flush()
    db.expire(note, ["text"])
    return note


def test_expired_attribute_is_not_loaded_silently(db):
    note = flushed_note(db)

    with pytest.raises(ImplicitLoadError):
        _ = note.text


def test_explicit_refresh_is_allowed(db):
    note = flushed_note(db)

    db.refresh(note)

    assert note.text == "hello"


@pytest.mark.anyio
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
