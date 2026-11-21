"""Шифрование заметок паролем.

Конвертная схема: пароль --Argon2id--> KEK, которым зашифрован случайный ключ данных DEK;
DEK шифрует текст и файлы. В БД — только соль с параметрами, зашифрованный DEK и шифротекст.
"""

import math
import os
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

import anyio
from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.settings import settings

KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16

# Каждое вычисление Argon2 занимает KDF_MEMORY_KIB памяти: без предела одновременные
# попытки подбора съели бы память сервера. Предел — на входе в поток: очередь ждёт в цикле
# событий и, в отличие от семафора внутри потока, не держит потоки пула
_kdf_limiter = anyio.CapacityLimiter(settings.KDF_MAX_CONCURRENT)


async def in_kdf_thread[T](fn: Callable[..., T], *args: Any) -> T:
    """Вызов с Argon2 внутри: ~0,1 с процессора не должны останавливать цикл событий."""
    return await anyio.to_thread.run_sync(fn, *args, limiter=_kdf_limiter)


class DecryptionError(Exception):
    """Неверный пароль или повреждённые данные — намеренно не различаем."""


@dataclass(frozen=True)
class KdfParams:
    salt: bytes
    time_cost: int
    memory_kib: int
    parallelism: int


@dataclass(frozen=True)
class EncryptedNote:
    # Параметры хранятся с заметкой: их можно ужесточить, а старые заметки откроются со своими
    kdf: KdfParams
    wrapped_key: bytes
    ciphertext: bytes


def new_kdf_params() -> KdfParams:
    return KdfParams(
        salt=os.urandom(SALT_BYTES),
        time_cost=settings.KDF_TIME_COST,
        memory_kib=settings.KDF_MEMORY_KIB,
        parallelism=settings.KDF_PARALLELISM,
    )


def derive_key(password: str, params: KdfParams) -> bytes:
    # NFC: «й» одним символом и «и» + кратка — один и тот же пароль
    secret = unicodedata.normalize("NFC", password).encode()
    return hash_secret_raw(
        secret=secret,
        salt=params.salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_kib,
        parallelism=params.parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """nonce + шифротекст + тег. AAD не шифруется, но без него не расшифровать."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise DecryptionError
    try:
        return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
    except InvalidTag:
        raise DecryptionError from None


# AAD привязывает шифротекст к заметке и к назначению: шифротекст одной заметки
# нельзя подложить в другую, а зашифрованный ключ — выдать за текст
def _key_aad(slug: str) -> bytes:
    return f"note-key:{slug}".encode()


def _text_aad(slug: str) -> bytes:
    return f"note-text:{slug}".encode()


@dataclass(frozen=True)
class OpenedNote:
    text: str
    # DEK: им же расшифровываются файлы заметки
    key: bytes


def new_note_key() -> bytes:
    return AESGCM.generate_key(bit_length=KEY_BYTES * 8)


def encrypt_note(password: str, text: str, slug: str, key: bytes | None = None) -> EncryptedNote:
    """key — DEK заметки; передают, когда им же нужно зашифровать файлы."""
    kdf = new_kdf_params()
    dek = new_note_key() if key is None else key
    return EncryptedNote(
        kdf=kdf,
        wrapped_key=encrypt(derive_key(password, kdf), dek, _key_aad(slug)),
        ciphertext=encrypt(dek, text.encode(), _text_aad(slug)),
    )


def unlock_note(password: str, encrypted: EncryptedNote, slug: str) -> OpenedNote:
    """Текст и ключ заметки. DecryptionError, если пароль неверный или данные повреждены.

    Отдельного хэша пароля нет: неверный пароль даёт другой KEK, и тег GCM не сходится.
    """
    dek = decrypt(derive_key(password, encrypted.kdf), encrypted.wrapped_key, _key_aad(slug))
    return OpenedNote(text=decrypt(dek, encrypted.ciphertext, _text_aad(slug)).decode(), key=dek)


# --- файлы ---
#
# Файл шифруется кусками: [кусок 0][кусок 1]...[кусок N], каждый — шифротекст и 16 байт тега.
# Кусок расшифровывается отдельно, поэтому Range читает с диска только нужные куски.
# Nonce куска — 8 случайных байт файла и номер куска: с одним DEK у файлов заметки они не
# совпадут. В AAD — id файла, номер и признак последнего куска: куски нельзя переставить,
# перенести в другой файл или отрезать хвост — обрезанный файл не сойдётся по тегу

FILE_NONCE_BYTES = 8


def new_file_nonce() -> bytes:
    return os.urandom(FILE_NONCE_BYTES)


@dataclass(frozen=True)
class FileCipher:
    key: bytes
    nonce: bytes
    file_id: str
    chunk_size: int

    @property
    def stored_chunk_size(self) -> int:
        return self.chunk_size + TAG_BYTES

    def _chunk_nonce(self, index: int) -> bytes:
        return self.nonce + index.to_bytes(4, "big")

    def _aad(self, index: int, last: bool) -> bytes:
        return f"note-file:{self.file_id}:".encode() + index.to_bytes(4, "big") + bytes([last])

    def encrypt_chunk(self, index: int, data: bytes, *, last: bool) -> bytes:
        return AESGCM(self.key).encrypt(self._chunk_nonce(index), data, self._aad(index, last))

    def decrypt_chunk(self, index: int, blob: bytes, *, last: bool) -> bytes:
        try:
            return AESGCM(self.key).decrypt(self._chunk_nonce(index), blob, self._aad(index, last))
        except InvalidTag:
            raise DecryptionError from None


def encrypt_file(cipher: FileCipher, source: BinaryIO) -> Iterator[bytes]:
    """Зашифрованные куски файла по порядку. Пустой файл — один пустой последний кусок."""
    index = 0
    chunk = source.read(cipher.chunk_size)
    while True:
        # Заглядываем на кусок вперёд: иначе не узнать, что текущий — последний
        following = source.read(cipher.chunk_size)
        yield cipher.encrypt_chunk(index, chunk, last=not following)
        if not following:
            return
        chunk = following
        index += 1


def stored_chunk_count(stored_size: int, cipher: FileCipher) -> int:
    return math.ceil(stored_size / cipher.stored_chunk_size)


def plain_size(stored_size: int, cipher: FileCipher) -> int:
    """Размер расшифрованного файла по размеру на диске. DecryptionError, если так не бывает."""
    count = stored_chunk_count(stored_size, cipher)
    tail = stored_size - (count - 1) * cipher.stored_chunk_size
    if count == 0 or tail < TAG_BYTES:
        raise DecryptionError
    return stored_size - count * TAG_BYTES


def decrypt_file(
    cipher: FileCipher, source: BinaryIO, stored_size: int, *, first: int = 0
) -> Iterator[bytes]:
    """Расшифрованные куски с номера first до конца файла.

    Последним считается кусок, на котором кончается файл, — признак сверяется тегом,
    поэтому файл, обрезанный ровно по границе куска, тоже даёт DecryptionError.
    """
    count = stored_chunk_count(stored_size, cipher)
    source.seek(first * cipher.stored_chunk_size)
    for index in range(first, count):
        blob = source.read(cipher.stored_chunk_size)
        yield cipher.decrypt_chunk(index, blob, last=index == count - 1)
