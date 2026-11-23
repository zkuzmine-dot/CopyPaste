"""Замер нагрузки до и после перехода на async.

Запуск из отдельного контейнера в сети compose (не делит процессор с app):
    docker run --rm -i --network copypaste_default python:3.13-slim sh -c
        "pip install -q httpx websockets && python - http://app:8000 40" < docs/load_check.py

Второй аргумент — сколько HTTP-запросов одновременно. Синхронная версия зависает на 30 с
уже при ~41: все потоки пула ждут соединение БД, а соединения держат запросы, которым нужен
поток для сериализации ответа (выход из get_db — после неё).
"""

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://app:8000"
WS_BASE = BASE.replace("http", "ws", 1)
HTTP_CONCURRENCY = int(sys.argv[2]) if len(sys.argv) > 2 else 40
NOTES = 20
GETS = 3000
CHANNELS = 100
MESSAGES_PER_CHANNEL = 10  # в пределах WS_RATE_LIMIT_BURST
WS_RATE_PER_SECOND = 2  # WS_RATE_LIMIT_PER_SECOND по умолчанию
WS_TIMEOUT = 60


@dataclass
class Result:
    latencies: list[float] = field(default_factory=list)
    errors: int = 0

    def add(self, other: "Result") -> None:
        self.latencies += other.latencies
        self.errors += other.errors


def summary(name: str, result: Result, elapsed: float) -> None:
    ordered = sorted(result.latencies) or [0.0]

    def pct(p: float) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * p))] * 1000

    print(
        f"{name:<28} ok={len(result.latencies):<5} errors={result.errors:<5} "
        f"{len(result.latencies) / elapsed:7.0f}/s  p50={pct(0.5):7.1f} ms  "
        f"p95={pct(0.95):7.1f} ms  p99={pct(0.99):7.1f} ms  wall={elapsed:6.1f} s",
        flush=True,
    )


async def create_notes(client: httpx.AsyncClient) -> list[str]:
    expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    slugs = []
    for i in range(NOTES):
        body = {"text": f"load {i}", "expires_at": expires}
        response = await client.post("/api/notes", json=body)
        response.raise_for_status()
        slugs.append(response.json()["slug"])
    return slugs


async def http_gets(client: httpx.AsyncClient, slugs: list[str]) -> Result:
    result = Result()
    queue: asyncio.Queue[str] = asyncio.Queue()
    for i in range(GETS):
        queue.put_nowait(slugs[i % len(slugs)])

    async def worker() -> None:
        while not queue.empty():
            slug = queue.get_nowait()
            started = time.perf_counter()
            try:
                response = await client.get(f"/api/notes/{slug}")
                response.raise_for_status()
            except httpx.HTTPError:
                result.errors += 1
                continue
            result.latencies.append(time.perf_counter() - started)

    await asyncio.gather(*(worker() for _ in range(HTTP_CONCURRENCY)))
    return result


async def authed(slug: str, token: str):
    ws = await websockets.connect(f"{WS_BASE}/ws/channels/{slug}")
    await ws.send(json.dumps({"type": "auth", "token": token}))
    state = json.loads(await ws.recv())
    assert state["type"] == "state", state
    return ws


async def open_channel(client: httpx.AsyncClient):
    created = (await client.post("/api/channels")).json()
    joined = (await client.post(f"/api/channels/{created['slug']}/join")).json()
    receiver = await authed(created["slug"], created["token"])
    sender = await authed(created["slug"], joined["token"])
    return sender, receiver


async def exchange(sender, receiver) -> Result:
    result = Result()

    async def receive(ws, record: bool) -> None:
        got = 0
        while got < MESSAGES_PER_CHANNEL:
            event = json.loads(await ws.recv())
            if event["type"] == "message":
                if record:
                    result.latencies.append(time.perf_counter() - float(event["text"]))
                got += 1

    try:
        async with asyncio.timeout(WS_TIMEOUT):
            # Отправитель тоже получает свои сообщения — читаем, чтобы не копился буфер
            readers = [
                asyncio.create_task(receive(receiver, record=True)),
                asyncio.create_task(receive(sender, record=False)),
            ]
            for _ in range(MESSAGES_PER_CHANNEL):
                await sender.send(json.dumps({"type": "send", "text": repr(time.perf_counter())}))
            await asyncio.gather(*readers)
    except (TimeoutError, websockets.ConnectionClosed):
        for reader in readers:
            reader.cancel()
        result.errors = MESSAGES_PER_CHANNEL - len(result.latencies)
    return result


async def ws_round(channels) -> Result:
    total = Result()
    for result in await asyncio.gather(*(exchange(s, r) for s, r in channels)):
        total.add(result)
    return total


async def main() -> None:
    limits = httpx.Limits(max_connections=HTTP_CONCURRENCY)
    async with httpx.AsyncClient(base_url=BASE, limits=limits, timeout=60) as client:
        slugs = await create_notes(client)
        await http_gets(client, slugs)  # прогрев

        started = time.perf_counter()
        summary("HTTP GET note", await http_gets(client, slugs), time.perf_counter() - started)

        started = time.perf_counter()
        channels = [await open_channel(client) for _ in range(CHANNELS)]
        print(f"{'open 100 channels (seq)':<28} wall={time.perf_counter() - started:.2f} s")

        started = time.perf_counter()
        summary("WS message delivery", await ws_round(channels), time.perf_counter() - started)

        # Лимит WebSocket: 10 событий подряд, дальше 2 в секунду. Ждём, пока запас
        # восстановится, иначе часть сообщений смешанного раунда получила бы rate_limited
        await asyncio.sleep(MESSAGES_PER_CHANNEL / WS_RATE_PER_SECOND)

        started = time.perf_counter()
        http_task = asyncio.create_task(http_gets(client, slugs))
        ws_result = await ws_round(channels)
        http_result = await http_task
        elapsed = time.perf_counter() - started
        summary("mixed: HTTP GET note", http_result, elapsed)
        summary("mixed: WS message delivery", ws_result, elapsed)

        for sender, receiver in channels:
            for ws in (sender, receiver):
                await ws.close()


asyncio.run(main())
