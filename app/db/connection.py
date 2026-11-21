from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import settings


def create_db_engine() -> AsyncEngine:
    # psycopg 3 работает и синхронно, и асинхронно — create_async_engine сам выбирает
    # асинхронный вариант драйвера для того же DATABASE_URL
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: после коммита атрибуты не протухают. В async их догрузка была бы
    # скрытым запросом и упала бы с MissingGreenlet
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# scope="function": коммит выполняется до отправки ответа, а не после
DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]


async def run_db[T](
    factory: async_sessionmaker[AsyncSession], fn: Callable[[AsyncSession], Awaitable[T]]
) -> T:
    """Короткая транзакция: commit, если fn завершилась, rollback, если упала.

    Для кода, который не держит DbSession всё время запроса: WebSocket, загрузка и
    скачивание файлов. fn возвращает готовые данные: связи не догружаются сами.
    """
    async with factory() as db, db.begin():
        return await fn(db)
