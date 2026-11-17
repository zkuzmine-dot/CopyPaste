from datetime import timedelta

from sqlalchemy import func, select, update

from app.db.models.channel import Channel, Message
from app.jobs.cleanup import run_channels_cleanup
from app.services import channels as channels_service
from app.settings import settings


def test_run_channels_cleanup(db, session_factory):
    receiver, _ = channels_service.create_channel(db)
    sender, _ = channels_service.join_channel(db, receiver.channel.slug)
    message_id = channels_service.add_message(db, sender, "text").id
    kept_channel_id = receiver.channel_id
    db.execute(update(Message).values(expires_at=func.now() - timedelta(seconds=1)))
    idle, _ = channels_service.create_channel(db)
    db.execute(
        update(Channel)
        .where(Channel.id == idle.channel_id)
        .values(
            last_activity_at=func.now()
            - timedelta(seconds=settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)
        )
    )
    db.commit()

    assert run_channels_cleanup(session_factory) == (1, 1)
    assert db.get(Message, message_id) is None
    assert db.scalars(select(Channel.id)).all() == [kept_channel_id]
