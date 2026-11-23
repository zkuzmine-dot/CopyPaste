import anyio
import pytest
from fastapi.testclient import TestClient

from app.db.redis import create_redis
from app.main import app
from app.security.file_keys import FileKeys
from app.settings import settings
from tests.conftest import TEST_REDIS_DB, TEST_REDIS_URL

pytestmark = pytest.mark.anyio


async def test_fixture_uses_separate_database(redis_client):
    assert redis_client.connection_pool.connection_kwargs["db"] == TEST_REDIS_DB

    await redis_client.set("key", "value")

    assert await redis_client.get("key") == b"value"


@pytest.mark.parametrize("run", [1, 2])
async def test_each_test_starts_with_empty_redis(redis_client, run):
    # Второй прогон упал бы, если бы фикстура не очищала базу между тестами
    assert await redis_client.dbsize() == 0
    await redis_client.set(f"left-by-run-{run}", 1)


async def test_client_fixture_uses_test_redis(client, redis_client):
    assert app.state.redis is redis_client


async def test_burst_waits_for_a_free_connection(monkeypatch):
    # Команд в 10 раз больше, чем соединений: лишние ждут свободное, а не падают
    monkeypatch.setattr(settings, "REDIS_MAX_CONNECTIONS", 3)
    client = create_redis(f"{TEST_REDIS_URL}?client_name=burst")
    replies: list[bool] = []

    async def ping() -> None:
        replies.append(await client.ping())

    try:
        async with anyio.create_task_group() as group:
            for _ in range(30):
                group.start_soon(ping)
        connections = [c for c in await client.client_list() if c["name"] == "burst"]
    finally:
        await client.aclose()

    assert replies == [True] * 30
    assert 1 <= len(connections) <= 3


async def test_new_connection_does_not_read_disk_in_loop():
    # Первая команда открывает соединение изнутри кода app — blockbuster увидел бы чтение
    # метаданных пакета, если бы redis-py искал свою версию сам
    client = create_redis(TEST_REDIS_URL)
    try:
        assert await FileKeys(client).get("missing") is None
    finally:
        await client.aclose()


async def _wait_until(connected: bool, connection_id: int) -> None:
    checker = create_redis(settings.REDIS_URL)
    try:
        # Сервер узнаёт о закрытии соединения не мгновенно — ждём недолго
        with anyio.fail_after(2):
            while bool(await checker.client_list(client_id=[connection_id])) != connected:
                await anyio.sleep(0.05)
    finally:
        await checker.aclose()


def test_lifespan_connects_and_closes_redis():
    # Клиент привязан к циклу событий, в котором создан: команды — через цикл TestClient
    with TestClient(app) as client:
        connection_id = client.portal.call(app.state.redis.client_id)
        anyio.run(_wait_until, True, connection_id)

    # Соединение закрыто при остановке, а не брошено открытым
    anyio.run(_wait_until, False, connection_id)
