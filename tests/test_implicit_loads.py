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


def test_participant_channel_is_never_lazy_loaded(db):
    created, _ = channels_service.create_channel(db)
    participant_id = created.id
    db.expunge_all()

    participant = db.get(Participant, participant_id)
    with pytest.raises(InvalidRequestError, match="lazy='raise'"):
        _ = participant.channel

    db.expunge_all()
    loaded = db.get(Participant, participant_id, options=[joinedload(Participant.channel)])
    assert loaded.channel.id == loaded.channel_id
