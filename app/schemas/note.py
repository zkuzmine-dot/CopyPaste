from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator

NOTE_TEXT_MAX_LENGTH = 100_000


def _must_be_future(value: datetime) -> datetime:
    if value <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    return value


# Ограничения только для входящих данных: NoteRead их не использует
NoteText = Annotated[str, Field(min_length=1, max_length=NOTE_TEXT_MAX_LENGTH)]
# AwareDatetime: без часового пояса (Z, +03:00) время не принимается
ExpiresAt = Annotated[AwareDatetime, AfterValidator(_must_be_future)]


class NoteCreate(BaseModel):
    text: NoteText
    expires_at: ExpiresAt | None = None


class NoteUpdate(BaseModel):
    text: NoteText | None = None
    expires_at: ExpiresAt | None = None

    @field_validator("text")
    @classmethod
    def text_not_null(cls, value: str | None) -> str:
        # Поле можно не передавать, но явный null запрещён: колонка NOT NULL
        if value is None:
            raise ValueError("text cannot be null")
        return value


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    text: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
