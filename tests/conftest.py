import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session

from app.db.connection import get_db
from app.main import app
from app.settings import settings

TEST_DATABASE_URL = make_url(settings.DATABASE_URL).set(
    database=f"{make_url(settings.DATABASE_URL).database}_test"
)


def _recreate_test_database() -> None:
    server_url = TEST_DATABASE_URL.set(database="postgres")
    engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE_URL.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DATABASE_URL.database}"'))
    engine.dispose()


def _apply_migrations() -> None:
    config = Config("alembic.ini")
    url = TEST_DATABASE_URL.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def engine():
    _recreate_test_database()
    _apply_migrations()
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine):
    # Весь тест — одна внешняя транзакция, которая в конце откатывается:
    # тесты не видят данные друг друга, а БД не нужно чистить
    with engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


@pytest.fixture
def client(connection):
    def override_get_db():
        # create_savepoint: commit/rollback сессии работают с SAVEPOINT, а не с внешней транзакцией
        db = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
