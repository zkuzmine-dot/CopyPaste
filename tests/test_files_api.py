import time
from datetime import timedelta
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from sqlalchemy import func, select, update

from app.db.models.file import StoredFile
from app.main import app
from app.security.upload_limit import UploadRateLimiter
from app.settings import settings
from app.utils import signing

pytestmark = pytest.mark.anyio

URL = "/api/files"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


async def upload(client, content=b"hello", name="hello.txt", **kwargs):
    return await client.post(URL, params={"name": name}, content=content, **kwargs)


async def uploaded_id(client, content=b"hello", name="hello.txt") -> str:
    response = await upload(client, content, name)
    assert response.status_code == 201
    return response.json()["id"]


def link(file_id: str, *, inline: bool = False, ttl: int = 60) -> str:
    return signing.file_url(file_id, int(time.time()) + ttl, inline=inline)


def link_query(file_id: str, *, inline: bool = False) -> dict[str, str]:
    query = urlsplit(link(file_id, inline=inline)).query
    return {key: values[0] for key, values in parse_qs(query).items()}


def files_on_disk(file_storage) -> list:
    root = file_storage.root
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


async def row(async_db, file_id: str) -> StoredFile | None:
    return await async_db.scalar(select(StoredFile).where(StoredFile.public_id == file_id))


async def mark_delete_after_download(async_db, file_id: str) -> None:
    await async_db.execute(
        update(StoredFile).where(StoredFile.public_id == file_id).values(delete_after_download=True)
    )


# --- загрузка ---


async def test_upload(client, async_db, file_storage):
    response = await upload(client, b"hello", "notes.txt")

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "id": body["id"],
        "name": "notes.txt",
        "size": 5,
        "content_type": "application/octet-stream",
        "is_image": False,
    }
    stored = await row(async_db, body["id"])
    assert stored.origin == "note"
    assert stored.note_id is None
    assert stored.keep_until - stored.created_at == timedelta(
        seconds=settings.PENDING_UPLOAD_TTL_SECONDS
    )
    assert file_storage.path(stored.storage_key).read_bytes() == b"hello"


@pytest.mark.parametrize(
    ("content", "claimed_type", "expected_type", "is_image"),
    [
        (PNG, "text/html", "image/png", True),
        (b"<html><script>alert(1)</script>", "image/png", "application/octet-stream", False),
    ],
    ids=["png_claimed_as_html", "html_claimed_as_png"],
)
async def test_type_is_detected_from_content(
    client, content, claimed_type, expected_type, is_image
):
    response = await upload(client, content, "x", headers={"Content-Type": claimed_type})

    assert response.json()["content_type"] == expected_type
    assert response.json()["is_image"] is is_image


async def test_upload_name_is_cleaned(client):
    response = await upload(client, name="../../etc/Отчёт за май.pdf")

    assert response.json()["name"] == "Отчёт за май.pdf"


async def test_upload_without_name_is_422(client):
    assert (await client.post(URL, content=b"hello")).status_code == 422


async def test_empty_upload_is_400(client, async_db, file_storage):
    response = await upload(client, b"")

    assert response.status_code == 400
    assert await async_db.scalar(select(func.count()).select_from(StoredFile)) == 0
    assert files_on_disk(file_storage) == []


async def test_upload_of_exact_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "FILE_MAX_SIZE_BYTES", 10)

    assert (await upload(client, b"x" * 10)).status_code == 201


async def test_too_large_by_content_length(client, async_db, file_storage, monkeypatch):
    monkeypatch.setattr(settings, "FILE_MAX_SIZE_BYTES", 10)

    response = await upload(client, b"x" * 11)

    assert response.status_code == 413
    assert await async_db.scalar(select(func.count()).select_from(StoredFile)) == 0
    assert files_on_disk(file_storage) == []


async def test_too_large_without_content_length(client, async_db, file_storage, monkeypatch):
    # Content-Length можно не прислать: тогда лимит срабатывает во время чтения потока
    monkeypatch.setattr(settings, "FILE_MAX_SIZE_BYTES", 10)

    async def chunks():
        yield b"x" * 6
        yield b"x" * 6

    response = await upload(client, chunks())

    assert response.status_code == 413
    assert await async_db.scalar(select(func.count()).select_from(StoredFile)) == 0
    assert files_on_disk(file_storage) == []


async def test_upload_rate_limit(client, redis_client):
    app.state.upload_rate_limiter = UploadRateLimiter(redis_client, rate=0.001, burst=2)

    assert [(await upload(client)).status_code for _ in range(3)] == [201, 201, 429]


async def test_storage_quota(client, monkeypatch):
    monkeypatch.setattr(settings, "FILES_MAX_TOTAL_BYTES", 10)

    assert (await upload(client, b"x" * 8)).status_code == 201
    assert (await upload(client, b"x" * 3)).status_code == 507
    assert (await upload(client, b"x" * 2)).status_code == 201


# --- скачивание ---


async def test_download(client):
    file_id = await uploaded_id(client, b"hello", "Отчёт.txt")

    response = await client.get(link(file_id))

    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == (
        f"attachment; filename=\"_____.txt\"; filename*=UTF-8''{quote('Отчёт.txt')}"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["cache-control"] == "private, no-store"


async def test_image_preview(client):
    file_id = await uploaded_id(client, PNG, "photo.png")

    response = await client.get(link(file_id, inline=True))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")


async def test_preview_only_for_images(client):
    file_id = await uploaded_id(client, b"<svg onload=alert(1)>", "image.svg")

    assert (await client.get(link(file_id, inline=True))).status_code == 400


async def test_range_request(client):
    file_id = await uploaded_id(client, b"hello")

    response = await client.get(link(file_id), headers={"Range": "bytes=0-1"})

    assert response.status_code == 206
    assert response.content == b"he"


@pytest.mark.parametrize(
    "bad_query",
    [
        lambda q, other: {**q, "sig": "A" * 43},
        lambda q, other: {**q, "exp": str(int(q["exp"]) + 60)},
        lambda q, other: {**q, "inline": "1"},
        lambda q, other: other,
        lambda q, other: {**q, "exp": str(int(time.time()) - 1)},
    ],
    ids=["forged", "extended", "inline_added", "other_files_link", "expired"],
)
async def test_bad_links_are_403(client, bad_query):
    file_id = await uploaded_id(client, PNG, "a.png")
    other_id = await uploaded_id(client, PNG, "b.png")

    response = await client.get(
        f"{URL}/{file_id}", params=bad_query(link_query(file_id), link_query(other_id))
    )

    assert response.status_code == 403


async def test_valid_link_to_missing_file_is_410(client):
    assert (await client.get(link("never-existed"))).status_code == 410


async def test_expired_pending_upload_is_410(client, async_db):
    file_id = await uploaded_id(client)
    await async_db.execute(
        update(StoredFile)
        .where(StoredFile.public_id == file_id)
        .values(keep_until=func.now() - timedelta(seconds=1))
    )

    assert (await client.get(link(file_id))).status_code == 410


# --- одноразовые файлы: удаляются только по подтверждению получения ---


def received(file_id: str, *, ttl: int = 60) -> str:
    return signing.received_url(file_id, int(time.time()) + ttl)


async def test_download_does_not_delete_one_time_file(client, async_db):
    # Сервер не знает, дошли ли байты до клиента, поэтому сам ничего не удаляет
    file_id = await uploaded_id(client, b"secret")
    await mark_delete_after_download(async_db, file_id)

    assert (await client.get(link(file_id))).content == b"secret"
    assert (await client.get(link(file_id))).content == b"secret"
    assert await row(async_db, file_id) is not None


async def test_range_works_for_one_time_file(client, async_db):
    # Докачка после обрыва — для медленного интернета
    file_id = await uploaded_id(client, b"secret")
    await mark_delete_after_download(async_db, file_id)

    response = await client.get(link(file_id), headers={"Range": "bytes=2-"})

    assert response.status_code == 206
    assert response.content == b"cret"


async def test_received_deletes_one_time_file(client, async_db, file_storage):
    file_id = await uploaded_id(client, b"secret")
    path = file_storage.path((await row(async_db, file_id)).storage_key)
    await mark_delete_after_download(async_db, file_id)

    assert (await client.post(received(file_id))).status_code == 204

    assert await row(async_db, file_id) is None
    assert not path.exists()
    assert (await client.get(link(file_id))).status_code == 410
    # Повтор ничего не ломает: клиент может переспросить, если не дождался ответа
    assert (await client.post(received(file_id))).status_code == 204


async def test_received_does_not_delete_regular_file(client, async_db):
    file_id = await uploaded_id(client, b"data")

    assert (await client.post(received(file_id))).status_code == 204
    assert await row(async_db, file_id) is not None


@pytest.mark.parametrize(
    "make_query",
    [
        lambda file_id: {**link_query(file_id), "sig": "A" * 43},
        lambda file_id: {**link_query(file_id), "exp": str(int(time.time()) - 1)},
        # Ссылка на превью не годится для подтверждения: подпись выдана под inline=1
        lambda file_id: {
            k: v for k, v in link_query(file_id, inline=True).items() if k != "inline"
        },
    ],
    ids=["forged", "expired", "preview_link"],
)
async def test_received_with_bad_link_is_403(client, async_db, make_query):
    file_id = await uploaded_id(client, PNG, "photo.png")
    await mark_delete_after_download(async_db, file_id)
    response = await client.post(f"{URL}/{file_id}/received", params=make_query(file_id))

    assert response.status_code == 403
    assert await row(async_db, file_id) is not None
