import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db.base import Base
from app.db.models.channel import Channel, Message, Participant  # noqa: F401
from app.db.models.file import StoredFile  # noqa: F401
from app.db.models.note import Note  # noqa: F401 — регистрирует модель в Base.metadata
from app.settings import settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
# "%" экранируется, т.к. configparser воспринимает его как интерполяцию
# Тесты передают свой URL (тестовая БД) — его не перезаписываем
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # Миграции запускаются и из кода (тесты): без этого флага fileConfig отключил бы
    # все логгеры приложения, созданные до миграций
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # Сами миграции синхронные (op.*): run_sync выполняет их поверх async-соединения
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Готовое соединение в config.attributes["connection"] передаёт тот, кто уже работает
    внутри event loop (там asyncio.run невозможен), — например, тесты через run_sync.
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
