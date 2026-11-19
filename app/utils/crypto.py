"""Шифрование заметок паролем.

Конвертная схема: пароль --Argon2id--> KEK, которым зашифрован случайный ключ данных DEK;
DEK шифрует текст. В БД — только соль с параметрами, зашифрованный DEK и шифротекст.
"""

import os
import threading
import unicodedata
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.settings import settings

KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16

# Каждое вычисление Argon2 занимает KDF_MEMORY_KIB памяти: без предела одновременные
# попытки подбора съели бы память сервера
_kdf_slots = threading.BoundedSemaphore(settings.KDF_MAX_CONCURRENT)


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
    with _kdf_slots:
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


def encrypt_note(password: str, text: str, slug: str) -> EncryptedNote:
    kdf = new_kdf_params()
    dek = AESGCM.generate_key(bit_length=KEY_BYTES * 8)
    return EncryptedNote(
        kdf=kdf,
        wrapped_key=encrypt(derive_key(password, kdf), dek, _key_aad(slug)),
        ciphertext=encrypt(dek, text.encode(), _text_aad(slug)),
    )


def unlock_note(password: str, encrypted: EncryptedNote, slug: str) -> str:
    """Текст заметки. DecryptionError, если пароль неверный или данные повреждены.

    Отдельного хэша пароля нет: неверный пароль даёт другой KEK, и тег GCM не сходится.
    """
    dek = decrypt(derive_key(password, encrypted.kdf), encrypted.wrapped_key, _key_aad(slug))
    return decrypt(dek, encrypted.ciphertext, _text_aad(slug)).decode()
