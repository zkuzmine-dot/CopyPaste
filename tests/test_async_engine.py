from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app


def test_lifespan_creates_async_sessions():
    with TestClient(app) as client:
        factory = app.state.async_session_factory

        async def select_one() -> int | None:
            async with factory() as db:
                return await db.scalar(text("SELECT 1"))

        # В цикле событий приложения: async-соединение привязано к циклу, где создано
        assert client.portal.call(select_one) == 1
        # Иначе после коммита атрибуты догружались бы скрытым запросом (MissingGreenlet)
        assert factory.kw["expire_on_commit"] is False
