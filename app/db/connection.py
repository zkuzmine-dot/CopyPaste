from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.settings import settings


def create_db_engine() -> Engine:
    return create_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
    )


def create_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine)


def create_async_db_engine() -> AsyncEngine:
    # Тот же DATABASE_URL: psycopg 3 работает и синхронно, и асинхронно —
    # create_async_engine сам выбирает асинхронный вариант драйвера
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
    )


def create_async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: после коммита атрибуты не протухают. В async их догрузка была бы
    # скрытым запросом и упала бы с MissingGreenlet
    return async_sessionmaker(engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# scope="function": коммит выполняется до отправки ответа, а не после
DbSession = Annotated[Session, Depends(get_db, scope="function")]


async def run_db[T](factory: sessionmaker[Session], fn: Callable[[Session], T]) -> T:
    """Короткая транзакция в пуле потоков.

    Для кода, который не может держать DbSession всё время запроса: WebSocket, загрузка и
    скачивание файлов. fn должна вернуть готовые данные — после коммита объекты ORM протухают.
    """

    def call() -> T:
        with factory() as db, db.begin():
            return fn(db)

    return await run_in_threadpool(call)
