import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
import pytest
from httpx2.websockets import ASGIWebSocketTransport
from sqlalchemy import text as sql

from app.db.redis import create_redis
from app.main import app
from app.security.attempts import AttemptsUnavailableError, PasswordAttempts
from app.settings import settings
from app.utils import crypto

pytestmark = pytest.mark.anyio

NOTES = "/api/notes"
FILES = "/api/files"
PASSWORD = "correct horse"
TEXT = "секретный текст заметки"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    # Настоящий Argon2 стоит ~0,1 с на вызов: здесь проверяем логику, а не стойкость
    monkeypatch.setattr(settings, "KDF_TIME_COST", 1)
    monkeypatch.setattr(settings, "KDF_MEMORY_KIB", 1024)
    monkeypatch.setattr(settings, "KDF_PARALLELISM", 1)


@pytest.fixture
def limits(monkeypatch):
    def set_limits(**values: int) -> None:
        for name, value in values.items():
            monkeypatch.setattr(settings, f"UNLOCK_{name}", value)

    return set_limits


@pytest.fixture
async def other_client(client):
    # Тот же сервер (состояние приложения выставила фикстура client), но с другого адреса
    transport = ASGIWebSocketTransport(app, client=("198.51.100.9", 50000))
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as other:
        yield other


async def created(client, **body) -> dict:
    response = await client.post(NOTES, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def protected(client, **extra) -> dict:
    return await created(client, text=TEXT, password=PASSWORD, **extra)


async def unlock(client, slug: str, password: str = PASSWORD):
    return await client.post(f"{NOTES}/{slug}/unlock", json={"password": password})


async def upload(client, content=b"hello", name="hello.txt") -> str:
    response = await client.post(FILES, params={"name": name}, content=content)
    assert response.status_code == 201
    return response.json()["id"]


# --- создание и хранение ---


async def test_create_protected_note(client):
    note = await protected(client)

    assert note["has_password"] is True
    assert note["text"] is None


async def test_text_is_not_stored_in_plain_form(client, db):
    note = await protected(client)

    row = await db.execute(sql("SELECT * FROM notes WHERE slug = :slug"), {"slug": note["slug"]})
    values = row.mappings().one()

    assert values["text"] == ""
    assert values["wrapped_key"] is not None
    assert values["ciphertext"] is not None
    assert (values["kdf_time_cost"], values["kdf_memory_kib"], values["kdf_parallelism"]) == (
        1,
        1024,
        1,
    )
    for value in values.values():
        if isinstance(value, bytes | memoryview):
            assert TEXT.encode() not in bytes(value)
        else:
            assert TEXT not in str(value)


async def test_get_hides_text(client):
    note = await protected(client)

    got = (await client.get(f"{NOTES}/{note['slug']}")).json()

    assert got["text"] is None
    assert got["has_password"] is True


async def test_regular_note_has_no_password(client):
    assert (await created(client, text="hello"))["has_password"] is False


async def test_note_with_only_files_and_password(client):
    note = await created(client, file_ids=[await upload(client)], password=PASSWORD)

    assert (await unlock(client, note["slug"])).json()["text"] == ""


# --- открытие ---


async def test_unlock(client):
    note = await protected(client)

    response = await unlock(client, note["slug"])

    assert response.status_code == 200
    assert response.json()["text"] == TEXT
    assert response.headers["cache-control"] == "no-store"


async def test_unlock_note_without_password_is_409(client):
    note = await created(client, text="hello")

    assert (await unlock(client, note["slug"])).status_code == 409


async def test_unlock_unknown_note_is_404(client):
    assert (await unlock(client, "zzzzzzzz")).status_code == 404


async def test_wrong_password_counts_down_then_freezes(client, limits):
    limits(NOTE_MAX_FAILURES=3, NOTE_LOCK_SECONDS=60, IP_MAX_FAILURES=100)
    note = await protected(client)

    left = [
        (await unlock(client, note["slug"], "wrong-password")).json()["detail"] for _ in range(3)
    ]

    assert [d["attempts_left"] for d in left] == [2, 1, 0]
    frozen = await unlock(client, note["slug"])
    assert frozen.status_code == 429
    assert 59 <= int(frozen.headers["retry-after"]) <= 60
    assert TEXT not in frozen.text


async def test_freezing_failure_says_how_long_to_wait(client, limits):
    limits(NOTE_MAX_FAILURES=2, NOTE_LOCK_SECONDS=60, IP_MAX_FAILURES=100)
    note = await protected(client)

    first = await unlock(client, note["slug"], "wrong-password")
    last = await unlock(client, note["slug"], "wrong-password")

    assert "retry-after" not in first.headers
    # Срок — сразу с ошибкой, включившей заморозку: таймер не ждёт следующей попытки
    assert last.status_code == 403
    assert last.json()["detail"]["attempts_left"] == 0
    assert 59 <= int(last.headers["retry-after"]) <= 60


async def test_freezing_failure_reports_longest_freeze(client, limits):
    # Одна ошибка заморозила и заметку, и IP: ждать до конца более долгой заморозки
    limits(NOTE_MAX_FAILURES=1, NOTE_LOCK_SECONDS=60, IP_MAX_FAILURES=1, IP_BAN_SECONDS=900)
    note = await protected(client)

    response = await unlock(client, note["slug"], "wrong-password")

    assert 899 <= int(response.headers["retry-after"]) <= 900


async def test_freezing_failure_without_redis_is_still_403(client, limits, monkeypatch):
    limits(NOTE_MAX_FAILURES=1, IP_MAX_FAILURES=100)
    note = await protected(client)
    original = PasswordAttempts.retry_after
    calls = []

    async def fails_after_check(self, ip, slug):
        calls.append(slug)
        if len(calls) > 1:
            raise AttemptsUnavailableError
        return await original(self, ip, slug)

    monkeypatch.setattr(PasswordAttempts, "retry_after", fails_after_check)

    response = await unlock(client, note["slug"], "wrong-password")

    # Проверка до Argon2 прошла, а срок заморозки узнать не вышло: ответ — всё ещё 403
    assert len(calls) == 2
    assert response.status_code == 403
    assert "retry-after" not in response.headers


async def test_correct_password_resets_note_counter(client, limits):
    limits(NOTE_MAX_FAILURES=3, IP_MAX_FAILURES=100)
    note = await protected(client)
    await unlock(client, note["slug"], "wrong-password")
    await unlock(client, note["slug"], "wrong-password")

    assert (await unlock(client, note["slug"])).status_code == 200

    detail = (await unlock(client, note["slug"], "wrong-password")).json()["detail"]
    assert detail["attempts_left"] == 2


async def test_frozen_ip_cannot_open_other_notes(client, other_client, limits):
    limits(IP_MAX_FAILURES=2, NOTE_MAX_FAILURES=100)
    first, second = await protected(client), await protected(client)
    for _ in range(2):
        await unlock(client, first["slug"], "wrong-password")

    assert (await unlock(client, second["slug"])).status_code == 429
    assert (await unlock(other_client, second["slug"])).status_code == 200


async def test_locked_note_cannot_be_opened_from_other_ip(client, other_client, limits):
    limits(NOTE_MAX_FAILURES=2, IP_MAX_FAILURES=100)
    note = await protected(client)
    for _ in range(2):
        await unlock(client, note["slug"], "wrong-password")

    assert (await unlock(other_client, note["slug"])).status_code == 429


async def test_frozen_check_happens_before_argon2(client, limits, monkeypatch):
    limits(NOTE_MAX_FAILURES=1, IP_MAX_FAILURES=100)
    note = await protected(client)
    await unlock(client, note["slug"], "wrong-password")
    calls = []
    original = crypto.derive_key
    monkeypatch.setattr(crypto, "derive_key", lambda *args: calls.append(args) or original(*args))

    assert (await unlock(client, note["slug"])).status_code == 429
    # Замороженный адрес не должен тратить память и CPU на Argon2
    assert calls == []


async def test_argon2_runs_outside_event_loop(client, monkeypatch):
    # ~0,1 с процессора на каждое вычисление: в потоке цикла событий оно остановило бы всех.
    # blockbuster этого не видит — Argon2 считается в C-коде
    threads = []
    original = crypto.derive_key

    def spy(*args):
        threads.append(threading.current_thread())
        return original(*args)

    monkeypatch.setattr(crypto, "derive_key", spy)

    note = await protected(client)
    await unlock(client, note["slug"])

    assert len(threads) == 2
    assert threading.main_thread() not in threads


async def test_unavailable_redis_is_503(client, unavailable_redis):
    note = await protected(client)
    # Напрямую, без monkeypatch: фикстура client сама уберёт app.state.redis после теста
    app.state.redis = unavailable_redis

    response = await unlock(client, note["slug"])

    assert response.status_code == 503
    assert TEXT not in response.text


async def test_hung_redis_does_not_stop_other_requests(committing_client, monkeypatch):
    note = await protected(committing_client)
    plain = await created(committing_client, text="открытая заметка")
    asked_redis = anyio.Event()
    original = PasswordAttempts.retry_after

    async def spy(self, ip, slug):
        asked_redis.set()
        return await original(self, ip, slug)

    monkeypatch.setattr(PasswordAttempts, "retry_after", spy)
    unlocked = {}

    async def unlock_note() -> None:
        started = time.perf_counter()
        unlocked["status"] = (await unlock(committing_client, note["slug"])).status_code
        unlocked["took"] = time.perf_counter() - started

    # Слушает, но не принимает: соединение устанавливается, а ответа нет — так выглядит
    # зависший Redis
    with anyio.fail_after(10), socket.create_server(("127.0.0.1", 0)) as silent:
        port = silent.getsockname()[1]
        app.state.redis = hung = create_redis(f"redis://127.0.0.1:{port}/0")
        try:
            async with anyio.create_task_group() as requests:
                requests.start_soon(unlock_note)
                await asked_redis.wait()
                started = time.perf_counter()
                other = await committing_client.get(f"{NOTES}/{plain['slug']}")
                other_took = time.perf_counter() - started
        finally:
            await hung.aclose()

    # Проверка пароля ждала ответа до таймаута и отказала, а цикл событий всё это время
    # обслуживал других
    assert unlocked["status"] == 503
    assert unlocked["took"] >= 1
    assert other.status_code == 200
    assert other_took < 0.5


# --- запрещённые действия ---


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/read", None),
        ("put", "", {"text": "changed"}),
        ("delete", "", None),
    ],
    ids=["read", "update", "delete"],
)
async def test_actions_without_password_are_409(client, method, path, body):
    note = await protected(client, burn_after_read=True)

    response = await client.request(method.upper(), f"{NOTES}/{note['slug']}{path}", json=body)

    assert response.status_code == 409
    # Ни одно действие не тронуло заметку — в том числе read не сжёг одноразовую
    assert (await unlock(client, note["slug"])).json()["text"] == TEXT


async def test_delete_with_password(client, limits):
    limits(NOTE_MAX_FAILURES=10, IP_MAX_FAILURES=10)
    note = await protected(client)
    url = f"{NOTES}/{note['slug']}/delete"

    assert (await client.post(url, json={"password": "wrong-password"})).status_code == 403
    assert (await client.get(f"{NOTES}/{note['slug']}")).status_code == 200

    assert (await client.post(url, json={"password": PASSWORD})).status_code == 204
    assert (await client.get(f"{NOTES}/{note['slug']}")).status_code == 404


@pytest.mark.parametrize(
    "password",
    ["q7z", "x" * (settings.NOTE_PASSWORD_MAX_LENGTH + 1)],
    ids=["too_short", "too_long"],
)
async def test_invalid_password_is_422_without_echo(client, password):
    response = await client.post(NOTES, json={"text": TEXT, "password": password})

    assert response.status_code == 422
    assert password not in response.text


# --- одноразовая заметка с паролем ---


async def test_burn_note_burns_only_after_correct_password(client):
    note = await protected(client, burn_after_read=True)

    assert (await unlock(client, note["slug"], "wrong-password")).status_code == 403
    assert (await client.get(f"{NOTES}/{note['slug']}")).status_code == 200

    assert (await unlock(client, note["slug"])).json()["text"] == TEXT
    assert (await client.get(f"{NOTES}/{note['slug']}")).status_code == 404
    assert (await unlock(client, note["slug"])).status_code == 404


async def test_burn_note_files_are_one_time(client):
    note = await protected(
        client, burn_after_read=True, file_ids=[await upload(client, PNG, "photo.png")]
    )

    [file] = (await unlock(client, note["slug"])).json()["files"]

    assert file["received_url"] is not None
    assert file["preview_url"] is None


# --- файлы ---


async def test_files_are_closed_until_password(client):
    note = await protected(client, file_ids=[await upload(client, PNG, "photo.png")])

    for files in (note["files"], (await client.get(f"{NOTES}/{note['slug']}")).json()["files"]):
        assert [(f["name"], f["download_url"], f["preview_url"]) for f in files] == [
            ("photo.png", None, None)
        ]


async def test_unlock_gives_short_lived_file_links(client):
    note = await protected(client, file_ids=[await upload(client, PNG, "photo.png")])
    started = int(time.time())

    [file] = (await unlock(client, note["slug"])).json()["files"]

    assert (await client.get(file["download_url"])).content == PNG
    assert (await client.get(file["preview_url"])).headers["content-type"] == "image/png"
    expires = int(parse_qs(urlsplit(file["download_url"]).query)["exp"][0])
    assert expires <= started + settings.PROTECTED_FILE_URL_TTL_SECONDS + 1
