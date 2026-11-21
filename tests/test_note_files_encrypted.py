import time
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.file import StoredFile
from app.db.redis import create_redis
from app.main import app
from app.services import files as files_service
from app.services import notes as notes_service
from app.settings import settings
from app.utils import crypto, signing

pytestmark = pytest.mark.anyio

NOTES = "/api/notes"
FILES = "/api/files"
PASSWORD = "correct horse"
CHUNK = 64
# 12 кусков по 64 байта: Range легко пересекает границы кусков
CONTENT = bytes(range(256)) * 3
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(200))


@pytest.fixture(autouse=True)
def fast_kdf_and_small_chunks(monkeypatch):
    monkeypatch.setattr(settings, "KDF_TIME_COST", 1)
    monkeypatch.setattr(settings, "KDF_MEMORY_KIB", 1024)
    monkeypatch.setattr(settings, "KDF_PARALLELISM", 1)
    monkeypatch.setattr(settings, "FILE_ENCRYPTION_CHUNK_BYTES", CHUNK)


async def upload(client, content=CONTENT, name="data.bin") -> str:
    response = await client.post(FILES, params={"name": name}, content=content)
    assert response.status_code == 201
    return response.json()["id"]


async def create(client, file_ids, **extra) -> dict:
    response = await client.post(NOTES, json={"file_ids": file_ids, "password": PASSWORD, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def unlock(client, slug: str) -> dict:
    response = await client.post(f"{NOTES}/{slug}/unlock", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


async def protected_file(client, content=CONTENT, name="data.bin", **extra) -> dict:
    """Ссылки на файл заметки с паролем — уже после ввода пароля."""
    note = await create(client, [await upload(client, content, name)], **extra)
    [file] = (await unlock(client, note["slug"]))["files"]
    return file


async def row(async_db, public_id: str) -> StoredFile:
    stored = await async_db.scalar(select(StoredFile).where(StoredFile.public_id == public_id))
    assert stored is not None
    # Строку меняли другие сессии — берём свежие значения
    await async_db.refresh(stored)
    return stored


def files_on_disk(file_storage) -> list:
    return [p for p in file_storage.root.glob("*/*/*") if p.is_file()]


def query_of(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


# --- хранение ---


async def test_files_are_encrypted_on_disk(client, async_db, file_storage):
    file_id = await upload(client)
    plain_key = (await row(async_db, file_id)).storage_key

    await create(client, [file_id])

    stored = await row(async_db, file_id)
    assert len(stored.encryption_nonce) == crypto.FILE_NONCE_BYTES
    assert stored.chunk_size == CHUNK
    # Копия под новым ключом, открытый исходник стёрт после коммита
    assert stored.storage_key != plain_key
    assert not file_storage.path(plain_key).exists()
    [path] = files_on_disk(file_storage)
    on_disk = path.read_bytes()
    assert len(on_disk) == len(CONTENT) + len(CONTENT) // CHUNK * crypto.TAG_BYTES
    assert CONTENT[:16] not in on_disk


async def test_regular_note_files_stay_plain(client, async_db, file_storage):
    file_id = await upload(client)
    plain_key = (await row(async_db, file_id)).storage_key

    response = await client.post(NOTES, json={"file_ids": [file_id]})

    assert response.status_code == 201
    stored = await row(async_db, file_id)
    assert (stored.storage_key, stored.encryption_nonce, stored.chunk_size) == (
        plain_key,
        None,
        None,
    )
    assert file_storage.path(plain_key).read_bytes() == CONTENT


# --- скачивание ---


async def test_download_is_decrypted(client):
    file = await protected_file(client)

    response = await client.get(file["download_url"])

    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["content-length"] == str(len(CONTENT))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_preview_is_decrypted(client):
    file = await protected_file(client, PNG, "photo.png")

    response = await client.get(file["preview_url"])

    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [
        ("bytes=100-299", 100, 299),
        ("bytes=64-127", 64, 127),
        ("bytes=0-0", 0, 0),
        ("bytes=700-", 700, 767),
        ("bytes=-10", 758, 767),
        ("bytes=760-5000", 760, 767),
    ],
    ids=["across_chunks", "exactly_one_chunk", "first_byte", "open_end", "suffix", "past_end"],
)
async def test_range(client, header, start, end):
    file = await protected_file(client)

    response = await client.get(file["download_url"], headers={"Range": header})

    assert response.status_code == 206
    assert response.content == CONTENT[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(CONTENT)}"
    assert response.headers["content-length"] == str(end - start + 1)


async def test_range_outside_file_is_416(client):
    file = await protected_file(client)

    response = await client.get(file["download_url"], headers={"Range": f"bytes={len(CONTENT)}-"})

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(CONTENT)}"


async def test_several_ranges_give_whole_file(client):
    file = await protected_file(client)

    response = await client.get(file["download_url"], headers={"Range": "bytes=0-1,5-6"})

    assert response.status_code == 200
    assert response.content == CONTENT


async def test_if_range(client):
    file = await protected_file(client)
    etag = (await client.get(file["download_url"])).headers["etag"]

    same = await client.get(file["download_url"], headers={"Range": "bytes=10-", "If-Range": etag})
    other = await client.get(
        file["download_url"], headers={"Range": "bytes=10-", "If-Range": '"x"'}
    )

    assert (same.status_code, same.content) == (206, CONTENT[10:])
    # Метка не та — у клиента начало другого файла, докачка с ним не склеится
    assert (other.status_code, other.content) == (200, CONTENT)


# --- повреждения ---


async def stored_path(async_db, file_storage, file: dict):
    return file_storage.path((await row(async_db, file["id"])).storage_key)


async def test_tampered_chunk_is_an_error_not_garbage(client, async_db, file_storage):
    file = await protected_file(client)
    path = await stored_path(async_db, file_storage, file)
    damaged = bytearray(path.read_bytes())
    damaged[3 * (CHUNK + crypto.TAG_BYTES) + 5] ^= 0x01
    path.write_bytes(damaged)

    # Куски проверяются по отдельности: целые отдаются, на подменённом — ошибка
    first_chunk = await client.get(file["download_url"], headers={"Range": f"bytes=0-{CHUNK - 1}"})
    assert first_chunk.content == CONTENT[:CHUNK]
    with pytest.raises(crypto.DecryptionError):
        await client.get(file["download_url"])


async def test_file_cut_at_chunk_boundary_is_an_error(client, async_db, file_storage):
    file = await protected_file(client)
    path = await stored_path(async_db, file_storage, file)
    # Все оставшиеся куски целые — обрезку выдаёт только признак последнего куска
    path.write_bytes(path.read_bytes()[: 5 * (CHUNK + crypto.TAG_BYTES)])

    with pytest.raises(crypto.DecryptionError):
        await client.get(file["download_url"])


async def test_missing_bytes_are_410(client, async_db, file_storage):
    file = await protected_file(client)
    (await stored_path(async_db, file_storage, file)).unlink()

    assert (await client.get(file["download_url"])).status_code == 410


# --- ключ в Redis ---


async def test_key_is_cached_only_for_notes_with_files(client, redis_client):
    plain = (await client.post(NOTES, json={"text": "без файлов", "password": PASSWORD})).json()
    await unlock(client, plain["slug"])
    assert list(redis_client.scan_iter("file-key:*")) == []

    await protected_file(client)
    assert len(list(redis_client.scan_iter("file-key:*"))) == 1


async def test_links_do_not_outlive_the_key(client, redis_client, monkeypatch):
    monkeypatch.setattr(settings, "FILE_KEY_CACHE_SECONDS", 60)
    started = int(time.time())

    file = await protected_file(client, PNG, "photo.png")

    [name] = redis_client.scan_iter("file-key:*")
    for url in (file["download_url"], file["preview_url"]):
        expires = int(query_of(url)["exp"])
        assert expires <= started + 61
        assert redis_client.expiretime(name) == expires


async def test_expired_key_needs_password_again(client, redis_client):
    note = await create(client, [await upload(client)])
    [file] = (await unlock(client, note["slug"]))["files"]
    redis_client.delete(f"file-key:{query_of(file['download_url'])['key_id']}")

    response = await client.get(file["download_url"])

    assert response.status_code == 403
    [again] = (await unlock(client, note["slug"]))["files"]
    assert (await client.get(again["download_url"])).content == CONTENT


async def test_key_id_cannot_be_swapped(client):
    first, second = await protected_file(client), await protected_file(client, b"other file")
    query = query_of(first["download_url"])
    query["key_id"] = query_of(second["download_url"])["key_id"]

    response = await client.get(f"{FILES}/{first['id']}?{urlencode(query)}")

    assert response.status_code == 403


async def test_link_without_key_id_is_403(client):
    file = await protected_file(client)
    expires = int(query_of(file["download_url"])["exp"])

    response = await client.get(signing.file_url(file["id"], expires))

    assert response.status_code == 403


async def test_unavailable_redis_on_download_is_503(client):
    file = await protected_file(client)
    host = urlsplit(settings.REDIS_URL).hostname
    # Напрямую, без monkeypatch: фикстура client сама уберёт app.state.redis после теста
    app.state.redis = create_redis(f"redis://{host}:6390/0")

    assert (await client.get(file["download_url"])).status_code == 503


# --- одноразовая заметка ---


async def test_burn_note_file_is_one_time(client, async_db, file_storage):
    file = await protected_file(client, burn_after_read=True)
    stored_key = (await row(async_db, file["id"])).storage_key

    assert (await client.get(file["download_url"])).content == CONTENT
    assert (await client.post(file["received_url"])).status_code == 204

    assert (await client.get(file["download_url"])).status_code == 410
    assert not file_storage.path(stored_key).exists()


# --- неудачное создание ---


async def test_failed_creation_leaves_no_copies(
    client, async_db, file_storage, async_session_factory, monkeypatch
):
    file_id = await upload(client)
    plain_key = (await row(async_db, file_id)).storage_key
    rival = (await client.post(NOTES, json={"text": "чужая"})).json()
    original = notes_service.encrypt_uploads

    async def attach_to_rival() -> None:
        async with async_session_factory() as other, other.begin():
            rival_note = await notes_service.get_note_by_slug(other, rival["slug"])
            await files_service.attach_to_note(other, rival_note.id, [file_id])

    def rival_attaches_meanwhile(*args):
        encrypted = original(*args)
        # Пока файл шифровался (в рабочем потоке), его успела забрать другая заметка
        anyio.from_thread.run(attach_to_rival)
        return encrypted

    monkeypatch.setattr(notes_service, "encrypt_uploads", rival_attaches_meanwhile)

    response = await client.post(NOTES, json={"file_ids": [file_id], "password": PASSWORD})

    assert response.status_code == 422
    # Зашифрованная копия стёрта, исходник на месте — он теперь у другой заметки
    assert files_on_disk(file_storage) == [file_storage.path(plain_key)]
    assert (await row(async_db, file_id)).storage_key == plain_key


async def test_upload_without_bytes_is_422(client, async_db, file_storage):
    file_id = await upload(client)
    file_storage.path((await row(async_db, file_id)).storage_key).unlink()

    response = await client.post(NOTES, json={"file_ids": [file_id], "password": PASSWORD})

    assert response.status_code == 422
    assert files_on_disk(file_storage) == []


# --- отмена и ошибки после коммита ---


def arm_after_commit(monkeypatch, on_close):
    """on_close(session) вызывается при закрытии сессии, где создали заметку, — после коммита."""
    armed = False
    original_create = notes_service.create_note
    original_close = AsyncSession.close

    async def create_and_arm(*args, **kwargs):
        nonlocal armed
        armed = True
        return await original_create(*args, **kwargs)

    async def close(self):
        nonlocal armed
        if armed:
            armed = False
            await on_close(self, original_close)
        else:
            await original_close(self)

    monkeypatch.setattr(notes_service, "create_note", create_and_arm)
    monkeypatch.setattr(AsyncSession, "close", close)


async def test_cancel_after_commit_still_cleans_up(client, async_db, file_storage, monkeypatch):
    file_id = await upload(client)
    plain_key = (await row(async_db, file_id)).storage_key
    closing = anyio.Event()

    async def slow_close(session, original_close):
        closing.set()
        await anyio.sleep(0.3)
        await original_close(session)

    arm_after_commit(monkeypatch, slow_close)

    async def create_note() -> None:
        await client.post(NOTES, json={"file_ids": [file_id], "password": PASSWORD})

    # Клиент ушёл, когда коммит уже прошёл, а уборка ещё нет
    async with anyio.create_task_group() as group:
        group.start_soon(create_note)
        await closing.wait()
        group.cancel_scope.cancel()

    stored = await row(async_db, file_id)
    assert stored.note_id is not None
    assert file_storage.path(stored.storage_key).exists()
    # Без защиты от отмены уборка не дошла бы до открытого исходника
    assert not file_storage.path(plain_key).exists()


async def test_error_after_commit_keeps_encrypted_copies(
    client, async_db, file_storage, monkeypatch
):
    file_id = await upload(client)

    async def broken_close(session, original_close):
        await original_close(session)
        raise ConnectionError("connection lost after commit")

    arm_after_commit(monkeypatch, broken_close)

    with pytest.raises(ConnectionError):
        await client.post(NOTES, json={"file_ids": [file_id], "password": PASSWORD})

    # Коммит прошёл: строка указывает на зашифрованную копию — стирать её нельзя
    stored = await row(async_db, file_id)
    assert stored.encryption_nonce is not None
    assert file_storage.path(stored.storage_key).exists()
