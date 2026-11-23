"""Ключи устройств канала.

Пары ECDH P-256 создают сами устройства (WebCrypto), сервер только хранит открытые ключи и
пересылает их собеседнику. Подключившееся устройство сначала присылает отпечаток своего ключа,
а сам ключ — только после ключа создателя. Так обе стороны фиксируют ключ раньше, чем видят
чужой, и сервер не может подобрать подменные ключи под совпадающие проверочные фигуры.
"""

import hashlib
import hmac

from cryptography.hazmat.primitives.asymmetric import ec

# Несжатая точка, как отдаёт WebCrypto exportKey("raw"): 0x04, затем X и Y по 32 байта
PUBLIC_KEY_BYTES = 65
COMMITMENT_BYTES = 32


def is_public_key(raw: bytes) -> bool:
    if len(raw) != PUBLIC_KEY_BYTES or raw[0] != 0x04:
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        return False
    return True


def commitment(public_key: bytes) -> bytes:
    return hashlib.sha256(public_key).digest()


def matches_commitment(public_key: bytes, expected: bytes) -> bool:
    return hmac.compare_digest(commitment(public_key), expected)
