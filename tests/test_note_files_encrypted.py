import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from sqlalchemy import select

from app.db.models.file import StoredFile
from app.db.redis import create_redis
from app.main import app
from app.services import files as files_service
from app.services import notes as notes_service
from app.settings import settings
from app.utils import crypto, signing

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


def upload(client, content=CONTENT, name="data.bin") -> str:
    response = client.post(FILES, params={"name": name}, content=content)
    assert response.status_code == 201
    return response.json()["id"]


def create(client, file_ids, **extra) -> dict:
    response = client.post(NOTES, json={"file_ids": file_ids, "password": PASSWORD, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def unlock(client, slug: str) -> dict:
    response = client.post(f"{NOTES}/{slug}/unlock", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def protected_file(client, content=CONTENT, name="data.bin", **extra) -> dict:
    """Ссылки на файл заметки с паролем — уже после ввода пароля."""
    note = create(client, [upload(client, content, name)], **extra)
    [file] = unlock(client, note["slug"])["files"]
    return file


def row(db, public_id: str) -> StoredFile:
    stored = db.scalar(select(StoredFile).where(StoredFile.public_id == public_id))
    assert stored is not None
    # Строку меняли другие сессии — берём свежие значения
    db.refresh(stored)
    return stored


def files_on_disk(file_storage) -> list:
    return [p for p in file_storage.root.glob("*/*/*") if p.is_file()]


def query_of(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


# --- хранение ---


def test_files_are_encrypted_on_disk(client, db, file_storage):
    file_id = upload(client)
    plain_key = row(db, file_id).storage_key

    create(client, [file_id])

    stored = row(db, file_id)
    assert len(stored.encryption_nonce) == crypto.FILE_NONCE_BYTES
    assert stored.chunk_size == CHUNK
    # Копия под новым ключом, открытый исходник стёрт после коммита
    assert stored.storage_key != plain_key
    assert not file_storage.path(plain_key).exists()
    [path] = files_on_disk(file_storage)
    on_disk = path.read_bytes()
    assert len(on_disk) == len(CONTENT) + len(CONTENT) // CHUNK * crypto.TAG_BYTES
    assert CONTENT[:16] not in on_disk


def test_regular_note_files_stay_plain(client, db, file_storage):
    file_id = upload(client)
    plain_key = row(db, file_id).storage_key

    response = client.post(NOTES, json={"file_ids": [file_id]})

    assert response.status_code == 201
    stored = row(db, file_id)
    assert (stored.storage_key, stored.encryption_nonce, stored.chunk_size) == (
        plain_key,
        None,
        None,
    )
    assert file_storage.path(plain_key).read_bytes() == CONTENT


# --- скачивание ---


def test_download_is_decrypted(client):
    file = protected_file(client)

    response = client.get(file["download_url"])

    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["content-length"] == str(len(CONTENT))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_preview_is_decrypted(client):
    file = protected_file(client, PNG, "photo.png")

    response = client.get(file["preview_url"])

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
def test_range(client, header, start, end):
    file = protected_file(client)

    response = client.get(file["download_url"], headers={"Range": header})

    assert response.status_code == 206
    assert response.content == CONTENT[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(CONTENT)}"
    assert response.headers["content-length"] == str(end - start + 1)


def test_range_outside_file_is_416(client):
    file = protected_file(client)

    response = client.get(file["download_url"], headers={"Range": f"bytes={len(CONTENT)}-"})

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(CONTENT)}"


def test_several_ranges_give_whole_file(client):
    file = protected_file(client)

    response = client.get(file["download_url"], headers={"Range": "bytes=0-1,5-6"})

    assert response.status_code == 200
    assert response.content == CONTENT


def test_if_range(client):
    file = protected_file(client)
    etag = client.get(file["download_url"]).headers["etag"]

    same = client.get(file["download_url"], headers={"Range": "bytes=10-", "If-Range": etag})
    other = client.get(file["download_url"], headers={"Range": "bytes=10-", "If-Range": '"x"'})

    assert (same.status_code, same.content) == (206, CONTENT[10:])
    # Метка не та — у клиента начало другого файла, докачка с ним не склеится
    assert (other.status_code, other.content) == (200, CONTENT)


# --- повреждения ---


def stored_path(db, file_storage, file: dict):
    return file_storage.path(row(db, file["id"]).storage_key)


def test_tampered_chunk_is_an_error_not_garbage(client, db, file_storage):
    file = protected_file(client)
    path = stored_path(db, file_storage, file)
    damaged = bytearray(path.read_bytes())
    damaged[3 * (CHUNK + crypto.TAG_BYTES) + 5] ^= 0x01
    path.write_bytes(damaged)

    # Куски проверяются по отдельности: целые отдаются, на подменённом — ошибка
    first_chunk = client.get(file["download_url"], headers={"Range": f"bytes=0-{CHUNK - 1}"})
    assert first_chunk.content == CONTENT[:CHUNK]
    with pytest.raises(crypto.DecryptionError):
        client.get(file["download_url"])


def test_file_cut_at_chunk_boundary_is_an_error(client, db, file_storage):
    file = protected_file(client)
    path = stored_path(db, file_storage, file)
    # Все оставшиеся куски целые — обрезку выдаёт только признак последнего куска
    path.write_bytes(path.read_bytes()[: 5 * (CHUNK + crypto.TAG_BYTES)])

    with pytest.raises(crypto.DecryptionError):
        client.get(file["download_url"])


def test_missing_bytes_are_410(client, db, file_storage):
    file = protected_file(client)
    stored_path(db, file_storage, file).unlink()

    assert client.get(file["download_url"]).status_code == 410


# --- ключ в Redis ---


def test_key_is_cached_only_for_notes_with_files(client, redis_client):
    plain = client.post(NOTES, json={"text": "без файлов", "password": PASSWORD}).json()
    unlock(client, plain["slug"])
    assert list(redis_client.scan_iter("file-key:*")) == []

    protected_file(client)
    assert len(list(redis_client.scan_iter("file-key:*"))) == 1


def test_links_do_not_outlive_the_key(client, redis_client, monkeypatch):
    monkeypatch.setattr(settings, "FILE_KEY_CACHE_SECONDS", 60)
    started = int(time.time())

    file = protected_file(client, PNG, "photo.png")

    [name] = redis_client.scan_iter("file-key:*")
    for url in (file["download_url"], file["preview_url"]):
        expires = int(query_of(url)["exp"])
        assert expires <= started + 61
        assert redis_client.expiretime(name) == expires


def test_expired_key_needs_password_again(client, redis_client):
    note = create(client, [upload(client)])
    [file] = unlock(client, note["slug"])["files"]
    redis_client.delete(f"file-key:{query_of(file['download_url'])['key_id']}")

    response = client.get(file["download_url"])

    assert response.status_code == 403
    [again] = unlock(client, note["slug"])["files"]
    assert client.get(again["download_url"]).content == CONTENT


def test_key_id_cannot_be_swapped(client):
    first, second = protected_file(client), protected_file(client, b"other file")
    query = query_of(first["download_url"])
    query["key_id"] = query_of(second["download_url"])["key_id"]

    response = client.get(f"{FILES}/{first['id']}?{urlencode(query)}")

    assert response.status_code == 403


def test_link_without_key_id_is_403(client):
    file = protected_file(client)
    expires = int(query_of(file["download_url"])["exp"])

    response = client.get(signing.file_url(file["id"], expires))

    assert response.status_code == 403


def test_unavailable_redis_on_download_is_503(client):
    file = protected_file(client)
    host = urlsplit(settings.REDIS_URL).hostname
    # Напрямую, без monkeypatch: фикстура client сама уберёт app.state.redis после теста
    app.state.redis = create_redis(f"redis://{host}:6390/0")

    assert client.get(file["download_url"]).status_code == 503


# --- одноразовая заметка ---


def test_burn_note_file_is_one_time(client, db, file_storage):
    file = protected_file(client, burn_after_read=True)
    stored_key = row(db, file["id"]).storage_key

    assert client.get(file["download_url"]).content == CONTENT
    assert client.post(file["received_url"]).status_code == 204

    assert client.get(file["download_url"]).status_code == 410
    assert not file_storage.path(stored_key).exists()


# --- неудачное создание ---


def test_failed_creation_leaves_no_copies(client, db, file_storage, session_factory, monkeypatch):
    file_id = upload(client)
    plain_key = row(db, file_id).storage_key
    rival = client.post(NOTES, json={"text": "чужая"}).json()
    original = notes_service.encrypt_uploads

    def rival_attaches_meanwhile(*args):
        encrypted = original(*args)
        # Пока файл шифровался, его успела забрать другая заметка
        with session_factory() as other, other.begin():
            rival_id = notes_service.get_note_by_slug(other, rival["slug"]).id
            files_service.attach_to_note(other, rival_id, [file_id])
        return encrypted

    monkeypatch.setattr(notes_service, "encrypt_uploads", rival_attaches_meanwhile)

    response = client.post(NOTES, json={"file_ids": [file_id], "password": PASSWORD})

    assert response.status_code == 422
    # Зашифрованная копия стёрта, исходник на месте — он теперь у другой заметки
    assert files_on_disk(file_storage) == [file_storage.path(plain_key)]
    assert row(db, file_id).storage_key == plain_key


def test_upload_without_bytes_is_422(client, db, file_storage):
    file_id = upload(client)
    file_storage.path(row(db, file_id).storage_key).unlink()

    response = client.post(NOTES, json={"file_ids": [file_id], "password": PASSWORD})

    assert response.status_code == 422
    assert files_on_disk(file_storage) == []
