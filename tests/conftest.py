import logging
import os
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from blockbuster import blockbuster_ctx
from httpx2.websockets import ASGIWebSocketTransport
from sqlalchemy import URL, create_engine, event, make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import ORMExecuteState, Session

from app.api.files.routes import create_upload_rate_limiter
from app.db.redis import create_redis
from app.main import app
from app.realtime.manager import ChannelManager
from app.settings import settings
from app.storage.local import LocalFileStorage

TEST_DATABASE_URL = make_url(settings.DATABASE_URL).set(
    database=f"{make_url(settings.DATABASE_URL).database}_test"
)
# Отдельная логическая база Redis: тесты не трогают счётчики и заморозки приложения
TEST_REDIS_DB = 15
TEST_REDIS_URL = urlsplit(settings.REDIS_URL)._replace(path=f"/{TEST_REDIS_DB}").geturl()


class ImplicitLoadError(AssertionError):
    pass


def _forbid_implicit_loads(state: ORMExecuteState) -> None:
    # Ленивая связь или протухший атрибут (после коммита, flush с SQL-выражением, bulk
    # UPDATE) догружаются скрытым SELECT. В async такой запрос падает с MissingGreenlet,
    # поэтому ловим его заранее. Явный refresh() — не скрытый, его пропускаем
    if state.is_relationship_load or (
        state.is_column_load and not state.load_options._is_user_refresh
    ):
        raise ImplicitLoadError(f"Implicit load: {state.statement}")


@pytest.fixture(autouse=True)
def no_implicit_loads():
    event.listen(Session, "do_orm_execute", _forbid_implicit_loads)
    yield
    event.remove(Session, "do_orm_execute", _forbid_implicit_loads)


def _on_server(url: URL, *statements: str) -> None:
    server = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        for statement in statements:
            conn.execute(text(statement))
    server.dispose()


def recreate_database(url: URL) -> None:
    _on_server(
        url,
        f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)',
        f'CREATE DATABASE "{url.database}"',
    )


def drop_database(url: URL) -> None:
    _on_server(url, f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')


def alembic_config(url: URL) -> Config:
    config = Config("alembic.ini")
    rendered = url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def test_database():
    recreate_database(TEST_DATABASE_URL)
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")


@pytest.fixture
def file_storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "files")


@pytest.fixture
def redis_client():
    client = create_redis(TEST_REDIS_URL)
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


# --- async: приложение и тест в одном цикле событий ---


# Debug-режим asyncio сообщает о шагах цикла дольше 0,1 с и падает, если цикл трогают из
# чужого потока. Но на каждый вызов в цикле он записывает стек, и на смонтированной папке
# Windows тесты идут вдвое дольше — поэтому по желанию: docker compose exec -e ASYNCIO_DEBUG=1
ASYNCIO_DEBUG = os.environ.get("ASYNCIO_DEBUG") == "1"


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio", {"debug": ASYNCIO_DEBUG}


def _is_async_test(request: pytest.FixtureRequest) -> bool:
    return request.node.get_closest_marker("anyio") is not None


@pytest.fixture(autouse=True)
def no_blocking_calls_in_loop(request):
    # Блокирующий вызов из кода app в цикле событий (sleep, файл, сокет, занятый замок)
    # останавливает весь сервер — тест падает с BlockingError. Код самих тестов и рабочие
    # потоки (to_thread, синхронные ручки) блокировать могут
    if not _is_async_test(request):
        yield
        return
    with blockbuster_ctx("app"):
        yield


class SlowLoopStepWarning(UserWarning):
    pass


class _SlowSteps(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("Executing"):
            self.messages.append(message)


@pytest.fixture(autouse=True)
def slow_loop_steps(request):
    # Долгий шаг цикла — предупреждение, а не ошибка: виноват может быть и сам тест,
    # например синхронный запрос в БД, а время на Docker Desktop нестабильно
    if not _is_async_test(request):
        yield []
        return
    handler = _SlowSteps()
    logger = logging.getLogger("asyncio")
    logger.addHandler(handler)
    yield handler.messages
    logger.removeHandler(handler)
    for message in handler.messages:
        warnings.warn(SlowLoopStepWarning(message), stacklevel=1)


@pytest.fixture(scope="session")
async def async_engine(test_database):
    async_engine = create_async_engine(TEST_DATABASE_URL)
    yield async_engine
    await async_engine.dispose()


@pytest.fixture
async def async_connection(async_engine):
    # Весь тест — одна внешняя транзакция, которая в конце откатывается:
    # тесты не видят данные друг друга, а БД не нужно чистить
    async with async_engine.connect() as connection:
        transaction = await connection.begin()
        yield connection
        await transaction.rollback()


@pytest.fixture
def async_session_factory(async_connection) -> async_sessionmaker[AsyncSession]:
    # create_savepoint: commit/rollback сессии работают с SAVEPOINT, а не с внешней транзакцией
    return async_sessionmaker(
        bind=async_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
async def async_db(async_session_factory):
    async with async_session_factory() as session:
        yield session


@asynccontextmanager
async def _serving(
    session_factory, file_storage, redis_client
) -> AsyncIterator[httpx2.AsyncClient]:
    # Приложение работает в цикле событий теста, поэтому ему подходят async-соединения
    # из фикстур. ASGIWebSocketTransport обслуживает и HTTP, и WebSocket. lifespan не
    # запускаем — состояние выставляем руками
    state = {
        "session_factory": session_factory,
        "file_storage": file_storage,
        "upload_rate_limiter": create_upload_rate_limiter(redis_client),
        "redis": redis_client,
        "channel_manager": ChannelManager(),
    }
    for name, value in state.items():
        setattr(app.state, name, value)
    try:
        transport = ASGIWebSocketTransport(app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        for name in state:
            delattr(app.state, name)


@pytest.fixture
async def client(async_session_factory, file_storage, redis_client):
    async with _serving(async_session_factory, file_storage, redis_client) as client:
        yield client


TABLES = ("notes", "channels", "participants", "messages", "stored_files")


@pytest.fixture
async def committing_session_factory(async_engine):
    """Настоящие коммиты: у каждой сессии своё соединение из пула.

    Нужно, когда приложение ходит в БД параллельно — WebSocket и таймер бездействия, гонки,
    одновременные запросы: одно тестовое соединение так не умеет. Данные видны всем
    соединениям, поэтому после теста таблицы очищаются.
    """
    yield async_sessionmaker(async_engine, expire_on_commit=False)
    async with async_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def committing_client(committing_session_factory, file_storage, redis_client):
    async with _serving(committing_session_factory, file_storage, redis_client) as client:
        yield client
