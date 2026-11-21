from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select, update

from app.db.models.file import FileOrigin, StoredFile
from app.db.models.note import Note
from app.jobs.cleanup import run_cleanup
from app.jobs.files import run_files_gc
from app.services import files as files_service
from app.settings import settings
from app.storage.base import new_storage_key

pytestmark = pytest.mark.anyio

NOTES = "/api/notes"
FILES = "/api/files"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


async def upload(client, content=b"hello", name="hello.txt") -> str:
    response = await client.post(FILES, params={"name": name}, content=content)
    assert response.status_code == 201
    return response.json()["id"]


async def create(client, **body):
    return await client.post(NOTES, json=body)


async def created(client, **body) -> dict:
    response = await create(client, **body)
    assert response.status_code == 201
    return response.json()


def link_expires(url: str) -> int:
    return int(parse_qs(urlsplit(url).query)["exp"][0])


async def stored(async_db, file_id: str) -> StoredFile | None:
    # populate_existing: строку меняют запросы в других сессиях, а объект из identity map
    # без этого вернулся бы со старыми значениями
    stmt = (
        select(StoredFile)
        .where(StoredFile.public_id == file_id)
        .execution_options(populate_existing=True)
    )
    return await async_db.scalar(stmt)


async def notes_count(async_db) -> int:
    return await async_db.scalar(select(func.count()).select_from(Note))


# --- создание ---


async def test_create_note_with_files(client, async_db):
    txt = await upload(client, b"hello", "notes.txt")
    png = await upload(client, PNG, "photo.png")

    note = await created(client, text="see files", file_ids=[png, txt])

    # Порядок — как в file_ids, а не как загружали
    assert [(f["id"], f["name"]) for f in note["files"]] == [(png, "photo.png"), (txt, "notes.txt")]
    photo, text_file = note["files"]
    assert photo["preview_url"] is not None
    assert text_file["preview_url"] is None
    assert (await client.get(text_file["download_url"])).content == b"hello"
    assert (await client.get(photo["preview_url"])).headers["content-type"] == "image/png"
    note_id = await async_db.scalar(select(Note.id).where(Note.slug == note["slug"]))
    rows = [await stored(async_db, png), await stored(async_db, txt)]
    assert [(r.note_id, r.position, r.keep_until) for r in rows] == [
        (note_id, 0, None),
        (note_id, 1, None),
    ]


async def test_regular_note_files_have_no_received_url(client):
    note = await created(client, text="x", file_ids=[await upload(client)])

    assert note["files"][0]["received_url"] is None


async def test_note_with_only_files(client):
    note = await created(client, file_ids=[await upload(client)])

    assert note["text"] == ""
    assert len(note["files"]) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": "", "file_ids": []},
        {"text": "x", "file_ids": [f"id-{i}" for i in range(settings.NOTE_MAX_FILES + 1)]},
        {"text": "x", "file_ids": ["same", "same"]},
        {"text": "x", "file_ids": [""]},
    ],
    ids=["nothing", "empty_text_no_files", "too_many_files", "duplicates", "empty_id"],
)
async def test_invalid_file_ids(client, async_db, body):
    assert (await create(client, **body)).status_code == 422
    assert await notes_count(async_db) == 0


async def _unknown(client, async_db) -> str:
    return "never-uploaded"


async def _attached(client, async_db) -> str:
    file_id = await upload(client)
    await created(client, text="first", file_ids=[file_id])
    return file_id


async def _expired(client, async_db) -> str:
    file_id = await upload(client)
    await async_db.execute(
        update(StoredFile)
        .where(StoredFile.public_id == file_id)
        .values(keep_until=func.now() - timedelta(seconds=1))
    )
    return file_id


async def _from_channel(client, async_db) -> str:
    channel_file = await files_service.create_upload(
        async_db,
        storage_key=new_storage_key(),
        name="a.txt",
        content_type="application/octet-stream",
        size_bytes=1,
        origin=FileOrigin.CHANNEL,
    )
    return channel_file.public_id


@pytest.mark.parametrize(
    "make_bad_file",
    [_unknown, _attached, _expired, _from_channel],
    ids=["unknown", "already_attached", "expired", "channel_file"],
)
async def test_unavailable_file_rejects_whole_note(client, async_db, make_bad_file):
    bad = await make_bad_file(client, async_db)
    good = await upload(client)
    notes_before = await notes_count(async_db)

    response = await create(client, text="x", file_ids=[good, bad])

    assert response.status_code == 422
    assert await notes_count(async_db) == notes_before
    # Откатилось всё: исправный файл тоже не привязан и ждёт дальше
    good_row = await stored(async_db, good)
    assert good_row.note_id is None
    assert good_row.keep_until is not None


# --- чтение и изменение ---


async def test_get_note_gives_fresh_links(client):
    note = await created(client, text="x", file_ids=[await upload(client, b"data")])

    files = (await client.get(f"{NOTES}/{note['slug']}")).json()["files"]

    assert (await client.get(files[0]["download_url"])).content == b"data"


async def test_links_do_not_outlive_note(client):
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    note = await created(
        client, text="x", file_ids=[await upload(client)], expires_at=expires_at.isoformat()
    )

    assert link_expires(note["files"][0]["download_url"]) <= int(expires_at.timestamp())


async def test_files_of_expired_note_are_unavailable(client, async_db):
    # Чистка ещё не прошла, заметка лежит в БД — но файл уже не отдаём
    note = await created(client, text="x", file_ids=[await upload(client)])
    await async_db.execute(
        update(Note)
        .where(Note.slug == note["slug"])
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert (await client.get(note["files"][0]["download_url"])).status_code == 410


async def test_update_keeps_files(client):
    note = await created(client, text="x", file_ids=[await upload(client)])

    updated = (await client.put(f"{NOTES}/{note['slug']}", json={"text": "y"})).json()

    assert [f["id"] for f in updated["files"]] == [f["id"] for f in note["files"]]


async def test_read_regular_note_gives_links(client):
    note = await created(client, text="x", file_ids=[await upload(client, b"data")])

    files = (await client.post(f"{NOTES}/{note['slug']}/read")).json()["files"]

    assert (await client.get(files[0]["download_url"])).content == b"data"


# --- удаление и истечение ---


async def test_deleted_note_files_are_collected(
    client, async_db, async_session_factory, file_storage
):
    file_id = await upload(client)
    path = file_storage.path((await stored(async_db, file_id)).storage_key)
    note = await created(client, text="x", file_ids=[file_id])

    assert (await client.delete(f"{NOTES}/{note['slug']}")).status_code == 204
    assert await run_files_gc(async_session_factory, file_storage) == 1
    assert not path.exists()


async def test_expired_note_files_are_collected(
    client, async_db, async_session_factory, file_storage
):
    file_id = await upload(client)
    path = file_storage.path((await stored(async_db, file_id)).storage_key)
    note = await created(client, text="x", file_ids=[file_id])
    await async_db.execute(
        update(Note)
        .where(Note.slug == note["slug"])
        .values(expires_at=func.now() - timedelta(seconds=1))
    )

    assert await run_cleanup(async_session_factory) == 1
    assert await run_files_gc(async_session_factory, file_storage) == 1
    assert not path.exists()


# --- одноразовые ---


async def test_burn_note_has_no_links_until_read(client):
    note = await created(
        client,
        text="secret",
        file_ids=[await upload(client, PNG, "photo.png")],
        burn_after_read=True,
    )

    got = (await client.get(f"{NOTES}/{note['slug']}")).json()

    for files in (note["files"], got["files"]):
        assert [(f["name"], f["download_url"], f["preview_url"]) for f in files] == [
            ("photo.png", None, None)
        ]
    assert got["text"] is None


async def test_burn_read_gives_one_time_links(client, async_db, file_storage):
    png = await upload(client, PNG, "photo.png")
    txt = await upload(client, b"hello", "notes.txt")
    txt_path = file_storage.path((await stored(async_db, txt)).storage_key)
    note = await created(client, text="secret", file_ids=[png, txt], burn_after_read=True)

    read = (await client.post(f"{NOTES}/{note['slug']}/read")).json()

    assert read["text"] == "secret"
    photo, text_file = read["files"]
    # Так договорились: у одноразовой заметки картинка — просто файл, без превью
    assert photo["preview_url"] is None
    assert text_file["received_url"] is not None
    row = await stored(async_db, txt)
    assert row.note_id is None
    assert row.delete_after_download is True
    assert link_expires(text_file["download_url"]) == int(row.keep_until.timestamp())
    assert row.keep_until - row.created_at == timedelta(
        seconds=settings.BURN_NOTE_FILES_WINDOW_SECONDS
    )

    # Скачивание само ничего не удаляет: оборванное можно повторить
    assert (await client.get(text_file["download_url"])).content == b"hello"
    assert await stored(async_db, txt) is not None

    assert (await client.post(text_file["received_url"])).status_code == 204
    assert (await client.get(text_file["download_url"])).status_code == 410
    assert await stored(async_db, txt) is None
    assert not txt_path.exists()
    # Получение второго файла не подтверждали — он на месте
    assert await stored(async_db, png) is not None


async def test_unread_burn_files_disappear_after_window(
    client, async_db, async_session_factory, file_storage
):
    note = await created(
        client, text="secret", file_ids=[await upload(client)], burn_after_read=True
    )
    url = (await client.post(f"{NOTES}/{note['slug']}/read")).json()["files"][0]["download_url"]
    await async_db.execute(update(StoredFile).values(keep_until=func.now() - timedelta(seconds=1)))

    assert await run_files_gc(async_session_factory, file_storage) == 1
    assert (await client.get(url)).status_code == 410
