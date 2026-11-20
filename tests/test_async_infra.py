import time

import anyio
import pytest
from blockbuster import BlockingError
from httpx2.websockets import WebSocketDisconnect
from sqlalchemy import func, select, text

from app.api.files import routes as files_routes
from app.db.models.note import Note
from app.services import notes as notes_service
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.anyio


async def test_async_db_uses_test_database(async_db):
    assert await async_db.scalar(text("SELECT current_database()")) == TEST_DATABASE_URL.database


async def test_session_commit_keeps_outer_transaction(async_db, async_connection):
    async_db.add(Note(text="hello"))
    await async_db.commit()

    # commit сессии — только savepoint: внешняя транзакция теста откатится в конце
    assert async_connection.in_transaction()
    assert await async_db.scalar(select(func.count()).select_from(Note)) == 1


async def test_blocking_call_from_app_in_loop_fails(async_client, monkeypatch):
    # Ручка скачивания — async: всё, что она вызывает, выполняется в цикле событий
    def slow_check(*args):
        time.sleep(0.01)

    monkeypatch.setattr(files_routes, "_check_link", slow_check)

    with pytest.raises(BlockingError, match="sleep"):
        await async_client.get("/api/files/abc", params={"exp": 1, "sig": "x"})


async def test_blocking_call_in_worker_thread_is_allowed(async_client, monkeypatch):
    # Синхронная ручка выполняется в пуле потоков — там блокироваться можно
    def slow_lookup(db, slug):
        time.sleep(0.01)

    monkeypatch.setattr(notes_service, "get_note_by_slug", slow_lookup)

    assert (await async_client.get("/api/notes/zzzzzzzz")).status_code == 404


async def test_test_code_may_block():
    # blockbuster смотрит только на код app: тест может, например, читать файл синхронно
    time.sleep(0.001)


async def test_websocket_in_test_loop(async_client):
    created = (await async_client.post("/api/channels")).json()

    async with async_client.websocket(f"/ws/channels/{created['slug']}") as ws:
        await ws.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as closed:
            await ws.receive_json()

    assert closed.value.code == 4401


async def test_slow_loop_step_is_reported(slow_loop_steps):
    time.sleep(0.15)
    await anyio.sleep(0)

    assert any("took" in message for message in slow_loop_steps)
    # Предупреждение здесь ожидаемое — не выносим его в отчёт
    slow_loop_steps.clear()
