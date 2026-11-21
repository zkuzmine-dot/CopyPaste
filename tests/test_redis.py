import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import TEST_REDIS_DB

pytestmark = pytest.mark.anyio


async def test_fixture_uses_separate_database(redis_client):
    assert redis_client.connection_pool.connection_kwargs["db"] == TEST_REDIS_DB

    redis_client.set("key", "value")

    assert redis_client.get("key") == b"value"


@pytest.mark.parametrize("run", [1, 2])
async def test_each_test_starts_with_empty_redis(redis_client, run):
    # Второй прогон упал бы, если бы фикстура не очищала базу между тестами
    assert redis_client.dbsize() == 0
    redis_client.set(f"left-by-run-{run}", 1)


async def test_client_fixture_uses_test_redis(client, redis_client):
    assert app.state.redis is redis_client


def test_lifespan_connects_to_redis():
    with TestClient(app):
        assert app.state.redis.ping() is True
