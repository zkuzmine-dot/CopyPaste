from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models.channel import Channel, Message
from app.jobs.cleanup import run_channels_cleanup
from app.services import channels as channels_service
from app.settings import settings

pytestmark = pytest.mark.anyio


async def test_run_channels_cleanup(async_db, async_session_factory):
    receiver, _ = await channels_service.create_channel(async_db)
    sender, _ = await channels_service.join_channel(async_db, receiver.channel.slug)
    message_id = (await channels_service.add_message(async_db, sender, "text")).id
    kept_channel_id = receiver.channel_id
    await async_db.execute(update(Message).values(expires_at=func.now() - timedelta(seconds=1)))
    idle, _ = await channels_service.create_channel(async_db)
    await async_db.execute(
        update(Channel)
        .where(Channel.id == idle.channel_id)
        .values(
            last_activity_at=func.now()
            - timedelta(seconds=settings.CHANNEL_IDLE_TIMEOUT_SECONDS + 1)
        )
    )
    await async_db.commit()

    assert await run_channels_cleanup(async_session_factory) == (1, 1)
    assert await async_db.get(Message, message_id) is None
    assert (await async_db.scalars(select(Channel.id))).all() == [kept_channel_id]
