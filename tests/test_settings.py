import pytest
from pydantic import ValidationError

from app.settings import Settings

DATABASE_URL = "postgresql+psycopg://user:password@db:5432/app"


@pytest.fixture
def make_settings(monkeypatch):
    # Только явные значения: без .env и без переменных окружения контейнера
    monkeypatch.delenv("FILES_SIGNING_SECRET", raising=False)

    def make(**values) -> Settings:
        return Settings(_env_file=None, DATABASE_URL=DATABASE_URL, **values)

    return make


def test_signing_secret_is_required(make_settings):
    with pytest.raises(ValidationError) as exc:
        make_settings()

    assert [(e["loc"], e["type"]) for e in exc.value.errors()] == [
        (("FILES_SIGNING_SECRET",), "missing")
    ]


def test_short_signing_secret_is_rejected(make_settings):
    with pytest.raises(ValidationError) as exc:
        make_settings(FILES_SIGNING_SECRET="x" * 31)

    assert exc.value.errors()[0]["type"] == "too_short"


def test_signing_secret_is_hidden(make_settings):
    secret = "s" * 32

    settings = make_settings(FILES_SIGNING_SECRET=secret)

    assert secret not in repr(settings)
    assert settings.FILES_SIGNING_SECRET.get_secret_value() == secret
