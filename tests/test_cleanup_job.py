from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models.channel import Channel, Message
from app.jobs.cleanup import run_channels_cleanup
from app.services import channels as channels_service
from app.settings import settings
from tests.channel_keys import fake_payload, join_ready

pytestmark = pytest.mark.anyio


async def test_run_channels_cleanup(db, session_factory):
    receiver, _ = await channels_service.create_channel(db)
    sender, _ = await join_ready(db, receiver.channel.slug)
    message_id = (await channels_service.add_message(db, sender, fake_payload("text"))).id
    kept_channel_id = receiver.channel_id
    await db.execute(update(Message).values(expires_at=func.now() - timedelta(seconds=1)))
    idle, _ = await channels_service.create_channel(db)
    await db.execute(
        update(Channel)
        .where(Channel.id == idle.channel_id)
        .values(
            last_activity_at=func.now()
            - timedelta(seconds=settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)
        )
    )
    await db.commit()

    assert await run_channels_cleanup(session_factory) == (1, 1)
    assert await db.get(Message, message_id) is None
    assert (await db.scalars(select(Channel.id))).all() == [kept_channel_id]
