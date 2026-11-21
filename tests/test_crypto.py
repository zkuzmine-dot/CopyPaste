import io
import math
import threading
import time
from dataclasses import replace

import anyio
import pytest

from app.settings import Settings, settings
from app.utils import crypto
from app.utils.crypto import DecryptionError, derive_key, encrypt_note, unlock_note

SLUG = "abcd2345"
PASSWORD = "correct horse"
TEXT = "Секретная заметка"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    # Настоящие параметры стоят ~0,1 с на вызов: здесь проверяем логику, а не стойкость
    monkeypatch.setattr(settings, "KDF_TIME_COST", 1)
    monkeypatch.setattr(settings, "KDF_MEMORY_KIB", 1024)
    monkeypatch.setattr(settings, "KDF_PARALLELISM", 1)


def flip(data: bytes, index: int) -> bytes:
    changed = bytearray(data)
    changed[index] ^= 0x01
    return bytes(changed)


# --- туда и обратно ---


@pytest.mark.parametrize(
    "text",
    ["Секретная заметка", "", "emoji 👋 и\nпереносы\tстрок", "я" * 100_000],
    ids=["cyrillic", "empty", "emoji", "long"],
)
def test_roundtrip(text):
    encrypted = encrypt_note(PASSWORD, text, SLUG)

    assert unlock_note(PASSWORD, encrypted, SLUG).text == text


def test_ciphertext_does_not_contain_text():
    encrypted = encrypt_note(PASSWORD, TEXT, SLUG)

    assert TEXT.encode() not in encrypted.ciphertext
    assert TEXT.encode() not in encrypted.wrapped_key


def test_same_input_gives_different_output():
    # Случайные соль, ключ данных и nonce: по шифротексту не видно, что заметки одинаковые
    first = encrypt_note(PASSWORD, TEXT, SLUG)
    second = encrypt_note(PASSWORD, TEXT, SLUG)

    assert first.kdf.salt != second.kdf.salt
    assert first.wrapped_key != second.wrapped_key
    assert first.ciphertext != second.ciphertext


# --- неверный пароль и повреждения ---


@pytest.mark.parametrize(
    "password",
    ["wrong password", "Correct horse", "correct horse ", ""],
    ids=["other", "case", "trailing_space", "empty"],
)
def test_wrong_password(password):
    encrypted = encrypt_note(PASSWORD, TEXT, SLUG)

    with pytest.raises(DecryptionError):
        unlock_note(password, encrypted, SLUG)


@pytest.mark.parametrize(
    ("field", "index"),
    [
        ("ciphertext", 0),
        ("ciphertext", 20),
        ("ciphertext", -1),
        ("wrapped_key", 0),
        ("wrapped_key", 20),
        ("wrapped_key", -1),
    ],
    ids=[
        "text_nonce",
        "text_body",
        "text_tag",
        "key_nonce",
        "key_body",
        "key_tag",
    ],
)
def test_tampering_is_detected(field, index):
    encrypted = encrypt_note(PASSWORD, TEXT, SLUG)
    tampered = replace(encrypted, **{field: flip(getattr(encrypted, field), index)})

    with pytest.raises(DecryptionError):
        unlock_note(PASSWORD, tampered, SLUG)


def test_changed_salt_is_detected():
    encrypted = encrypt_note(PASSWORD, TEXT, SLUG)
    tampered = replace(encrypted, kdf=replace(encrypted.kdf, salt=flip(encrypted.kdf.salt, 0)))

    with pytest.raises(DecryptionError):
        unlock_note(PASSWORD, tampered, SLUG)


@pytest.mark.parametrize(
    "length", [-1, 27, 0], ids=["minus_one_byte", "shorter_than_nonce_and_tag", "empty"]
)
def test_truncated_ciphertext(length):
    encrypted = encrypt_note(PASSWORD, TEXT, SLUG)
    truncated = replace(encrypted, ciphertext=encrypted.ciphertext[:length])

    with pytest.raises(DecryptionError):
        unlock_note(PASSWORD, truncated, SLUG)


def test_data_copied_to_another_note_does_not_open():
    # Кто-то с доступом к БД скопировал зашифрованные колонки одной заметки в другую.
    # Пароль верный, но шифротекст привязан к slug — подмену видно
    encrypted = encrypt_note(PASSWORD, TEXT, SLUG)

    with pytest.raises(DecryptionError):
        unlock_note(PASSWORD, encrypted, "zzzz2345")


# --- пароль и параметры ---


def test_password_unicode_forms_are_equal():
    composed = "пароль-й"  # «й» одним символом
    decomposed = "пароль-й"  # «и» + кратка
    encrypted = encrypt_note(composed, TEXT, SLUG)

    assert unlock_note(decomposed, encrypted, SLUG).text == TEXT


def test_stored_params_are_used(monkeypatch):
    encrypted = encrypt_note(PASSWORD, TEXT, SLUG)
    # Параметры по умолчанию ужесточили — старая заметка открывается со своими
    monkeypatch.setattr(settings, "KDF_TIME_COST", 2)
    monkeypatch.setattr(settings, "KDF_MEMORY_KIB", 2048)

    assert encrypted.kdf.time_cost == 1
    assert encrypted.kdf.memory_kib == 1024
    assert unlock_note(PASSWORD, encrypted, SLUG).text == TEXT


def test_derive_key():
    params = crypto.new_kdf_params()

    key = derive_key(PASSWORD, params)

    assert len(key) == crypto.KEY_BYTES
    assert derive_key(PASSWORD, params) == key
    assert derive_key(PASSWORD, replace(params, salt=flip(params.salt, 0))) != key


def test_default_params_work(monkeypatch):
    for name in ("KDF_TIME_COST", "KDF_MEMORY_KIB", "KDF_PARALLELISM"):
        monkeypatch.setattr(settings, name, Settings.model_fields[name].default)

    encrypted = encrypt_note(PASSWORD, TEXT, SLUG)

    assert (encrypted.kdf.time_cost, encrypted.kdf.memory_kib, encrypted.kdf.parallelism) == (
        10,
        65_536,
        4,
    )
    assert unlock_note(PASSWORD, encrypted, SLUG).text == TEXT


# --- предел одновременных вычислений ---


@pytest.mark.anyio
async def test_concurrent_kdf_is_limited(monkeypatch):
    monkeypatch.setattr(crypto, "_kdf_limiter", anyio.CapacityLimiter(2))
    active = 0
    max_active = 0
    lock = threading.Lock()
    threads = set()

    def slow_hash(**kwargs) -> bytes:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            threads.add(threading.current_thread())
        time.sleep(0.1)
        with lock:
            active -= 1
        return b"k" * crypto.KEY_BYTES

    monkeypatch.setattr(crypto, "hash_secret_raw", slow_hash)
    params = crypto.new_kdf_params()

    async with anyio.create_task_group() as group:
        for _ in range(6):
            group.start_soon(crypto.in_kdf_thread, derive_key, PASSWORD, params)

    # Не больше двух сразу — и не в цикле событий: в его потоке нельзя считать Argon2
    assert max_active == 2
    assert threading.main_thread() not in threads


def test_unlock_returns_key_of_the_note():
    key = crypto.new_note_key()
    encrypted = encrypt_note(PASSWORD, TEXT, SLUG, key)

    assert unlock_note(PASSWORD, encrypted, SLUG).key == key


# --- файлы кусками ---

CHUNK = 16
FILE_ID = "Ab3dEf6hIj9kLm2nOp5qRs"


def file_cipher(**changes) -> crypto.FileCipher:
    cipher = crypto.FileCipher(
        key=b"k" * crypto.KEY_BYTES,
        nonce=b"n" * crypto.FILE_NONCE_BYTES,
        file_id=FILE_ID,
        chunk_size=CHUNK,
    )
    return replace(cipher, **changes)


def encrypted_file(cipher: crypto.FileCipher, plain: bytes) -> bytes:
    return b"".join(crypto.encrypt_file(cipher, io.BytesIO(plain)))


def decrypted_file(cipher: crypto.FileCipher, stored: bytes, first: int = 0) -> bytes:
    chunks = crypto.decrypt_file(cipher, io.BytesIO(stored), len(stored), first=first)
    return b"".join(chunks)


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 3 * CHUNK + 5])
def test_file_roundtrip(size):
    cipher = file_cipher()
    plain = bytes(i % 251 for i in range(size))

    stored = encrypted_file(cipher, plain)

    # Каждый кусок — плюс тег; даже у пустого файла есть один кусок с признаком «последний»
    assert len(stored) == size + max(1, math.ceil(size / CHUNK)) * crypto.TAG_BYTES
    assert crypto.plain_size(len(stored), cipher) == size
    assert decrypted_file(cipher, stored) == plain


def test_file_ciphertext_does_not_contain_plaintext():
    plain = b"secret file bytes" * 20

    assert b"secret file" not in encrypted_file(file_cipher(), plain)


def test_chunks_decrypt_without_reading_previous_ones():
    cipher = file_cipher()
    plain = bytes(range(100))
    stored = encrypted_file(cipher, plain)

    # Range с середины: первые два куска не читаются вовсе
    assert decrypted_file(cipher, stored, first=2) == plain[2 * CHUNK :]


STORED_CHUNK = CHUNK + crypto.TAG_BYTES


def swap_chunks(stored: bytes) -> bytes:
    first, second = stored[:STORED_CHUNK], stored[STORED_CHUNK : 2 * STORED_CHUNK]
    return second + first + stored[2 * STORED_CHUNK :]


def chunk_from_other_file(stored: bytes) -> bytes:
    # Тот же ключ заметки, но другой файл: кусок с тем же номером подложить нельзя
    other = encrypted_file(file_cipher(nonce=b"m" * 8, file_id="other-file-id"), bytes(64))
    return (
        stored[:STORED_CHUNK] + other[STORED_CHUNK : 2 * STORED_CHUNK] + stored[2 * STORED_CHUNK :]
    )


@pytest.mark.parametrize(
    "damage",
    [
        lambda stored: flip(stored, STORED_CHUNK + 3),
        swap_chunks,
        chunk_from_other_file,
        # Ровно по границе куска: каждый оставшийся кусок цел, выдаёт обрезку только признак
        lambda stored: stored[: 2 * STORED_CHUNK],
        lambda stored: stored[:-1],
        lambda stored: stored + stored[-STORED_CHUNK:],
    ],
    ids=["flipped_byte", "swapped", "foreign_chunk", "cut_at_boundary", "cut_inside", "appended"],
)
def test_damaged_file_is_detected(damage):
    cipher = file_cipher()
    stored = encrypted_file(cipher, bytes(range(64)))

    with pytest.raises(DecryptionError):
        decrypted_file(cipher, damage(stored))


@pytest.mark.parametrize(
    "changes",
    [{"key": b"x" * crypto.KEY_BYTES}, {"nonce": b"x" * 8}, {"file_id": "other-file-id"}],
    ids=["other_key", "other_nonce", "other_file"],
)
def test_file_needs_its_own_key_nonce_and_id(changes):
    stored = encrypted_file(file_cipher(), bytes(40))

    with pytest.raises(DecryptionError):
        decrypted_file(file_cipher(**changes), stored)


@pytest.mark.parametrize("stored_size", [0, crypto.TAG_BYTES - 1, STORED_CHUNK + 3])
def test_impossible_stored_size(stored_size):
    # Последний кусок короче тега — такого файла шифрование не создаёт
    with pytest.raises(DecryptionError):
        crypto.plain_size(stored_size, file_cipher())
