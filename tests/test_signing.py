from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from app.settings import settings
from app.utils import signing

EXPIRES = 2_000_000_000
NOW = 1_900_000_000


def parse(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    return parts.path, {key: values[0] for key, values in parse_qs(parts.query).items()}


def test_file_url():
    path, query = parse(signing.file_url("abc", EXPIRES))

    assert path == "/api/files/abc"
    assert query["exp"] == str(EXPIRES)
    assert "inline" not in query
    assert signing.is_valid_signature("abc", EXPIRES, False, query["sig"], now=NOW)


def test_inline_url_is_signed_separately():
    _, query = parse(signing.file_url("abc", EXPIRES, inline=True))

    assert query["inline"] == "1"
    assert signing.is_valid_signature("abc", EXPIRES, True, query["sig"], now=NOW)
    assert not signing.is_valid_signature("abc", EXPIRES, False, query["sig"], now=NOW)


def test_expiration():
    _, query = parse(signing.file_url("abc", EXPIRES))

    assert signing.is_valid_signature("abc", EXPIRES, False, query["sig"], now=EXPIRES)
    assert not signing.is_valid_signature("abc", EXPIRES, False, query["sig"], now=EXPIRES + 1)


@pytest.mark.parametrize(
    ("public_id", "expires", "signature"),
    [
        ("other", EXPIRES, None),
        ("abc", EXPIRES + 1, None),
        ("abc", EXPIRES, "A" * 43),
        ("abc", EXPIRES, "подпись"),
        ("abc", EXPIRES, ""),
    ],
    ids=["other_file", "other_expiry", "forged", "non_ascii", "empty"],
)
def test_invalid_signatures(public_id, expires, signature):
    _, query = parse(signing.file_url("abc", EXPIRES))

    assert not signing.is_valid_signature(
        public_id, expires, False, signature if signature is not None else query["sig"], now=NOW
    )


def test_signature_depends_on_secret(monkeypatch):
    _, query = parse(signing.file_url("abc", EXPIRES))

    monkeypatch.setattr(settings, "FILES_SIGNING_SECRET", SecretStr("another-secret-" * 3))

    assert not signing.is_valid_signature("abc", EXPIRES, False, query["sig"], now=NOW)


def test_key_id_is_signed():
    path, query = parse(signing.file_url("abc", EXPIRES, key_id="key-1"))

    assert path == "/api/files/abc"
    assert query["key_id"] == "key-1"
    assert signing.is_valid_signature("abc", EXPIRES, False, query["sig"], key_id="key-1", now=NOW)
    # Запись с ключом подменить нельзя, и выбросить её из ссылки тоже
    for other in ("key-2", None):
        assert not signing.is_valid_signature(
            "abc", EXPIRES, False, query["sig"], key_id=other, now=NOW
        )


def test_received_url_is_signed_without_key_id():
    _, download = parse(signing.file_url("abc", EXPIRES, key_id="key-1"))
    _, received = parse(signing.received_url("abc", EXPIRES))

    assert "key_id" not in received
    assert received["sig"] != download["sig"]
    assert signing.is_valid_signature("abc", EXPIRES, False, received["sig"], now=NOW)
