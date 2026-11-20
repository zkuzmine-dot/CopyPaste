import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql

from app.db.redis import create_redis
from app.main import app
from app.settings import settings
from app.utils import crypto

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
def other_client(client):
    # Тот же сервер, но запросы с другого адреса
    return TestClient(app, client=("198.51.100.9", 50000))


def created(client, **body) -> dict:
    response = client.post(NOTES, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def protected(client, **extra) -> dict:
    return created(client, text=TEXT, password=PASSWORD, **extra)


def unlock(client, slug: str, password: str = PASSWORD):
    return client.post(f"{NOTES}/{slug}/unlock", json={"password": password})


def upload(client, content=b"hello", name="hello.txt") -> str:
    response = client.post(FILES, params={"name": name}, content=content)
    assert response.status_code == 201
    return response.json()["id"]


# --- создание и хранение ---


def test_create_protected_note(client):
    note = protected(client)

    assert note["has_password"] is True
    assert note["text"] is None


def test_text_is_not_stored_in_plain_form(client, db):
    note = protected(client)

    row = db.execute(sql("SELECT * FROM notes WHERE slug = :slug"), {"slug": note["slug"]})
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


def test_get_hides_text(client):
    note = protected(client)

    got = client.get(f"{NOTES}/{note['slug']}").json()

    assert got["text"] is None
    assert got["has_password"] is True


def test_regular_note_has_no_password(client):
    assert created(client, text="hello")["has_password"] is False


def test_note_with_only_files_and_password(client):
    note = created(client, file_ids=[upload(client)], password=PASSWORD)

    assert unlock(client, note["slug"]).json()["text"] == ""


# --- открытие ---


def test_unlock(client):
    note = protected(client)

    response = unlock(client, note["slug"])

    assert response.status_code == 200
    assert response.json()["text"] == TEXT
    assert response.headers["cache-control"] == "no-store"


def test_unlock_note_without_password_is_409(client):
    note = created(client, text="hello")

    assert unlock(client, note["slug"]).status_code == 409


def test_unlock_unknown_note_is_404(client):
    assert unlock(client, "zzzzzzzz").status_code == 404


def test_wrong_password_counts_down_then_freezes(client, limits):
    limits(NOTE_MAX_FAILURES=3, NOTE_LOCK_SECONDS=60, IP_MAX_FAILURES=100)
    note = protected(client)

    left = [unlock(client, note["slug"], "wrong-password").json()["detail"] for _ in range(3)]

    assert [d["attempts_left"] for d in left] == [2, 1, 0]
    frozen = unlock(client, note["slug"])
    assert frozen.status_code == 429
    assert 59 <= int(frozen.headers["retry-after"]) <= 60
    assert TEXT not in frozen.text


def test_correct_password_resets_note_counter(client, limits):
    limits(NOTE_MAX_FAILURES=3, IP_MAX_FAILURES=100)
    note = protected(client)
    unlock(client, note["slug"], "wrong-password")
    unlock(client, note["slug"], "wrong-password")

    assert unlock(client, note["slug"]).status_code == 200

    detail = unlock(client, note["slug"], "wrong-password").json()["detail"]
    assert detail["attempts_left"] == 2


def test_frozen_ip_cannot_open_other_notes(client, other_client, limits):
    limits(IP_MAX_FAILURES=2, NOTE_MAX_FAILURES=100)
    first, second = protected(client), protected(client)
    for _ in range(2):
        unlock(client, first["slug"], "wrong-password")

    assert unlock(client, second["slug"]).status_code == 429
    assert unlock(other_client, second["slug"]).status_code == 200


def test_locked_note_cannot_be_opened_from_other_ip(client, other_client, limits):
    limits(NOTE_MAX_FAILURES=2, IP_MAX_FAILURES=100)
    note = protected(client)
    for _ in range(2):
        unlock(client, note["slug"], "wrong-password")

    assert unlock(other_client, note["slug"]).status_code == 429


def test_frozen_check_happens_before_argon2(client, limits, monkeypatch):
    limits(NOTE_MAX_FAILURES=1, IP_MAX_FAILURES=100)
    note = protected(client)
    unlock(client, note["slug"], "wrong-password")
    calls = []
    original = crypto.unlock_note
    monkeypatch.setattr(crypto, "unlock_note", lambda *args: calls.append(args) or original(*args))

    assert unlock(client, note["slug"]).status_code == 429
    # Замороженный адрес не должен тратить память и CPU на Argon2
    assert calls == []


def test_unavailable_redis_is_503(client):
    note = protected(client)
    host = urlsplit(settings.REDIS_URL).hostname
    # Напрямую, без monkeypatch: фикстура client сама уберёт app.state.redis после теста
    app.state.redis = create_redis(f"redis://{host}:6390/0")

    response = unlock(client, note["slug"])

    assert response.status_code == 503
    assert TEXT not in response.text


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
def test_actions_without_password_are_409(client, method, path, body):
    note = protected(client, burn_after_read=True)

    response = client.request(method.upper(), f"{NOTES}/{note['slug']}{path}", json=body)

    assert response.status_code == 409
    # Ни одно действие не тронуло заметку — в том числе read не сжёг одноразовую
    assert unlock(client, note["slug"]).json()["text"] == TEXT


def test_delete_with_password(client, limits):
    limits(NOTE_MAX_FAILURES=10, IP_MAX_FAILURES=10)
    note = protected(client)
    url = f"{NOTES}/{note['slug']}/delete"

    assert client.post(url, json={"password": "wrong-password"}).status_code == 403
    assert client.get(f"{NOTES}/{note['slug']}").status_code == 200

    assert client.post(url, json={"password": PASSWORD}).status_code == 204
    assert client.get(f"{NOTES}/{note['slug']}").status_code == 404


@pytest.mark.parametrize(
    "password",
    ["q7z", "x" * (settings.NOTE_PASSWORD_MAX_LENGTH + 1)],
    ids=["too_short", "too_long"],
)
def test_invalid_password_is_422_without_echo(client, password):
    response = client.post(NOTES, json={"text": TEXT, "password": password})

    assert response.status_code == 422
    assert password not in response.text


# --- одноразовая заметка с паролем ---


def test_burn_note_burns_only_after_correct_password(client):
    note = protected(client, burn_after_read=True)

    assert unlock(client, note["slug"], "wrong-password").status_code == 403
    assert client.get(f"{NOTES}/{note['slug']}").status_code == 200

    assert unlock(client, note["slug"]).json()["text"] == TEXT
    assert client.get(f"{NOTES}/{note['slug']}").status_code == 404
    assert unlock(client, note["slug"]).status_code == 404


def test_burn_note_files_are_one_time(client):
    note = protected(client, burn_after_read=True, file_ids=[upload(client, PNG, "photo.png")])

    [file] = unlock(client, note["slug"]).json()["files"]

    assert file["received_url"] is not None
    assert file["preview_url"] is None


# --- файлы ---


def test_files_are_closed_until_password(client):
    note = protected(client, file_ids=[upload(client, PNG, "photo.png")])

    for files in (note["files"], client.get(f"{NOTES}/{note['slug']}").json()["files"]):
        assert [(f["name"], f["download_url"], f["preview_url"]) for f in files] == [
            ("photo.png", None, None)
        ]


def test_unlock_gives_short_lived_file_links(client):
    note = protected(client, file_ids=[upload(client, PNG, "photo.png")])
    started = int(time.time())

    [file] = unlock(client, note["slug"]).json()["files"]

    assert client.get(file["download_url"]).content == PNG
    assert client.get(file["preview_url"]).headers["content-type"] == "image/png"
    expires = int(parse_qs(urlsplit(file["download_url"]).query)["exp"][0])
    assert expires <= started + settings.PROTECTED_FILE_URL_TTL_SECONDS + 1
