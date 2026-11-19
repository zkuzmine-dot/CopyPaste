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
