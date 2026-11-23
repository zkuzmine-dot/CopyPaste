import anyio
import pytest

pytestmark = pytest.mark.anyio

REQUESTS = 60


async def test_many_concurrent_requests_do_not_stall(committing_client):
    # Синхронная версия зависала на 30 с уже при 41+ одновременном запросе: все потоки пула
    # ждали соединение с БД, а соединения держали запросы, ждавшие поток для сериализации
    # ответа. Пул соединений здесь тот же: 5 + 10
    client = committing_client
    slug = (await client.post("/api/notes", json={"text": "hello"})).json()["slug"]
    statuses: list[int] = []

    async def get() -> None:
        statuses.append((await client.get(f"/api/notes/{slug}")).status_code)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            for _ in range(REQUESTS):
                group.start_soon(get)

    assert statuses == [200] * REQUESTS
