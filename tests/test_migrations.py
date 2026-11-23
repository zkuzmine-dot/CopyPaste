import asyncio
from collections.abc import Callable

import pytest
import sqlalchemy.ext.asyncio
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, pool

from app.db.base import Base
from app.db.models import channel, file, note  # noqa: F401 — регистрируют модели в Base.metadata
from tests.conftest import TEST_DATABASE_URL, alembic_config, drop_database, recreate_database

# Своя база: здесь миграции откатываются до нуля, общей тестовой базе это бы помешало
MIGRATIONS_URL = TEST_DATABASE_URL.set(database=f"{TEST_DATABASE_URL.database}_migrations")


@pytest.fixture
def config():
    recreate_database(MIGRATIONS_URL)
    yield alembic_config(MIGRATIONS_URL)
    drop_database(MIGRATIONS_URL)


def on_async_connection[T](fn: Callable[[Connection], T]) -> T:
    """Синхронная fn поверх async-соединения — так же, как env.py выполняет миграции."""

    async def main() -> T:
        engine = sqlalchemy.ext.asyncio.create_async_engine(MIGRATIONS_URL, poolclass=pool.NullPool)
        try:
            async with engine.begin() as connection:
                return await connection.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def tables(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def head(config: Config) -> str | None:
    return ScriptDirectory.from_config(config).get_current_head()


def test_upgrade_downgrade_upgrade(config):
    command.upgrade(config, "head")
    assert on_async_connection(tables) >= {
        "notes",
        "channels",
        "participants",
        "messages",
        "stored_files",
    }

    command.downgrade(config, "base")
    assert on_async_connection(tables) == {"alembic_version"}

    command.upgrade(config, "head")
    assert on_async_connection(current_revision) == head(config)


def test_models_match_migrations(config):
    command.upgrade(config, "head")

    diff = on_async_connection(
        lambda connection: compare_metadata(MigrationContext.configure(connection), Base.metadata)
    )

    # То же, что alembic check: новая миграция из autogenerate была бы пустой
    assert diff == []


def test_migrations_run_over_async_connection(config, monkeypatch):
    engines = []
    original = sqlalchemy.ext.asyncio.async_engine_from_config

    def spy(*args, **kwargs):
        engine = original(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "async_engine_from_config", spy)

    command.upgrade(config, "head")

    assert [engine.dialect.is_async for engine in engines] == [True]


def test_migrations_use_given_connection(config):
    # Внутри event loop asyncio.run невозможен: env.py должен взять переданное соединение
    def upgrade(connection: Connection) -> None:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    on_async_connection(upgrade)

    assert on_async_connection(current_revision) == head(config)
