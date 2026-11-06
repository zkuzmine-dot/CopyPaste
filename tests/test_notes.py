from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, update

from app.db.models.note import Note
from app.schemas.note import NOTE_TEXT_MAX_LENGTH
from app.utils.slug import SLUG_ALPHABET, SLUG_LENGTH

URL = "/api/notes"


def future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.fixture
def note(client) -> dict:
    response = client.post(URL, json={"text": "hello"})
    assert response.status_code == 201
    return response.json()


def expire(connection, slug: str) -> None:
    # Через API прошлый expires_at не задать (422), поэтому правим строку напрямую
    connection.execute(
        update(Note).where(Note.slug == slug).values(expires_at=func.now() - timedelta(seconds=1))
    )


# --- POST ---

def test_create_note(client):
    response = client.post(URL, json={"text": "hello"})

    assert response.status_code == 201
    body = response.json()
    assert body["text"] == "hello"
    assert body["expires_at"] is None
    assert len(body["slug"]) == SLUG_LENGTH
    assert set(body["slug"]) <= set(SLUG_ALPHABET)
    assert "id" not in body


def test_create_note_with_expires_at(client):
    expires_at = future()

    response = client.post(URL, json={"text": "hello", "expires_at": expires_at})

    assert response.status_code == 201
    assert datetime.fromisoformat(response.json()["expires_at"]) == datetime.fromisoformat(expires_at)


def test_create_note_max_length(client):
    response = client.post(URL, json={"text": "a" * NOTE_TEXT_MAX_LENGTH})

    assert response.status_code == 201


# --- GET ---

def test_get_note(client, note):
    response = client.get(f"{URL}/{note['slug']}")

    assert response.status_code == 200
    assert response.json() == note


# --- PUT ---

def test_update_text(client, note):
    response = client.put(f"{URL}/{note['slug']}", json={"text": "updated"})

    assert response.status_code == 200
    assert response.json()["text"] == "updated"
    assert client.get(f"{URL}/{note['slug']}").json()["text"] == "updated"


def test_update_only_expires_at_keeps_text(client, note):
    response = client.put(f"{URL}/{note['slug']}", json={"expires_at": future()})

    assert response.status_code == 200
    assert response.json()["text"] == "hello"
    assert response.json()["expires_at"] is not None


def test_update_expires_at_null_removes_expiry(client):
    slug = client.post(URL, json={"text": "hello", "expires_at": future()}).json()["slug"]

    response = client.put(f"{URL}/{slug}", json={"expires_at": None})

    assert response.status_code == 200
    assert response.json()["expires_at"] is None


# --- DELETE ---

def test_delete_note(client, note):
    response = client.delete(f"{URL}/{note['slug']}")

    assert response.status_code == 204
    assert client.get(f"{URL}/{note['slug']}").status_code == 404


# --- 404 ---

@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("put", {"text": "x"}), ("delete", None)],
)
def test_unknown_slug_returns_404(client, method, body):
    kwargs = {"json": body} if body else {}

    response = client.request(method, f"{URL}/zzzzzzzz", **kwargs)

    assert response.status_code == 404
    assert response.json() == {"detail": "Note not found"}


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("put", {"text": "x"}), ("delete", None)],
)
def test_expired_note_returns_404(client, connection, note, method, body):
    expire(connection, note["slug"])
    kwargs = {"json": body} if body else {}

    response = client.request(method, f"{URL}/{note['slug']}", **kwargs)

    assert response.status_code == 404


# --- 422 ---

def test_create_too_long_text(client):
    response = client.post(URL, json={"text": "a" * (NOTE_TEXT_MAX_LENGTH + 1)})

    assert response.status_code == 422


def test_update_too_long_text(client, note):
    response = client.put(f"{URL}/{note['slug']}", json={"text": "a" * (NOTE_TEXT_MAX_LENGTH + 1)})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": ""},
        {"text": 123},
        {"text": "x", "expires_at": "2020-01-01T00:00:00Z"},
        {"text": "x", "expires_at": "2030-01-01T00:00:00"},
        {"text": "x", "expires_at": "завтра"},
    ],
    ids=["no-text", "empty-text", "text-not-string", "expires-in-past", "expires-no-tz", "expires-garbage"],
)
def test_create_invalid_body(client, body):
    response = client.post(URL, json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"text": None},
        {"text": ""},
        {"expires_at": "2020-01-01T00:00:00Z"},
    ],
    ids=["text-null", "empty-text", "expires-in-past"],
)
def test_update_invalid_body(client, note, body):
    response = client.put(f"{URL}/{note['slug']}", json=body)

    assert response.status_code == 422
