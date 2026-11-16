import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from enum import IntEnum
from typing import Any

from fastapi import WebSocket
from pydantic import BaseModel


class CloseCode(IntEnum):
    UNAUTHORIZED = 4401
    CHANNEL_NOT_FOUND = 4404
    REPLACED = 4409
    CHANNEL_CLOSED = 4410


class Connection:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        # До отправки state события копятся здесь: иначе событие, случившееся пока
        # грузится state, придёт раньше него и затрётся устаревшим снимком
        self._pending: list[str] | None = []

    async def send(self, event: BaseModel) -> None:
        data = event.model_dump_json()
        if self._pending is not None:
            self._pending.append(data)
        else:
            await self._send_text(data)

    async def start(self, state: BaseModel) -> None:
        await self._send_text(state.model_dump_json())
        while self._pending:
            await self._send_text(self._pending.pop(0))
        self._pending = None

    async def close(self, code: int) -> None:
        # Соединение могло уже оборваться — тогда его обработчик сам всё уберёт
        with contextlib.suppress(Exception):
            await self.websocket.close(code)

    async def _send_text(self, data: str) -> None:
        with contextlib.suppress(Exception):
            await self.websocket.send_text(data)


class ChannelManager:
    """Кто сейчас подключён к каким каналам. Живёт в памяти, поэтому только один процесс app."""

    def __init__(self) -> None:
        self._rooms: dict[str, dict[int, Connection]] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}

    async def connect(self, slug: str, participant_id: int, connection: Connection) -> bool:
        """Возвращает True, если участник уже был онлайн и старое соединение заменено."""
        room = self._rooms.setdefault(slug, {})
        old = room.get(participant_id)
        room[participant_id] = connection
        if old is not None:
            await old.close(CloseCode.REPLACED)
        return old is not None

    def disconnect(self, slug: str, participant_id: int, connection: Connection) -> bool:
        room = self._rooms.get(slug)
        if room is None or room.get(participant_id) is not connection:
            return False
        del room[participant_id]
        if not room:
            del self._rooms[slug]
            self._stop_watcher(slug)
        return True

    def participant_ids(self, slug: str) -> list[int]:
        return list(self._rooms.get(slug, {}))

    def is_peer_online(self, slug: str, participant_id: int) -> bool:
        return any(pid != participant_id for pid in self._rooms.get(slug, {}))

    async def send_to(self, slug: str, participant_id: int, event: BaseModel) -> None:
        connection = self._rooms.get(slug, {}).get(participant_id)
        if connection is not None:
            await connection.send(event)

    async def broadcast(self, slug: str, event: BaseModel, exclude: int | None = None) -> None:
        for pid, connection in list(self._rooms.get(slug, {}).items()):
            if pid != exclude:
                await connection.send(event)

    def watch(self, slug: str, make_watcher: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if slug not in self._watchers:
            self._watchers[slug] = asyncio.create_task(make_watcher())

    async def close_room(self, slug: str, event: BaseModel, code: int) -> None:
        self._stop_watcher(slug)
        for connection in self._rooms.pop(slug, {}).values():
            await connection.send(event)
            await connection.close(code)

    def _stop_watcher(self, slug: str) -> None:
        watcher = self._watchers.pop(slug, None)
        # Комнату может закрывать сам watcher — себя он не отменяет
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
