from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from app.schemas.file import LinkedFileRead
from app.settings import settings

NOTE_TEXT_MAX_LENGTH = 100_000


def _must_be_future(value: datetime) -> datetime:
    if value <= datetime.now(UTC):
        raise ValueError("expires_at must be in the future")
    return value


# Ограничения только для входящих данных: NoteRead их не использует
NoteText = Annotated[str, Field(min_length=1, max_length=NOTE_TEXT_MAX_LENGTH)]
# AwareDatetime: без часового пояса (Z, +03:00) время не принимается
ExpiresAt = Annotated[AwareDatetime, AfterValidator(_must_be_future)]


FileId = Annotated[str, Field(min_length=1, max_length=64)]
# SecretStr: пароль не попадёт в repr и логи. Длина ограничена и при вводе: слишком длинный
# пароль — это лишняя работа Argon2, а слишком короткий всё равно не может быть верным
Password = Annotated[
    SecretStr,
    Field(
        min_length=settings.NOTE_PASSWORD_MIN_LENGTH,
        max_length=settings.NOTE_PASSWORD_MAX_LENGTH,
    ),
]


class NoteCreate(BaseModel):
    text: Annotated[str, Field(max_length=NOTE_TEXT_MAX_LENGTH)] = ""
    file_ids: Annotated[list[FileId], Field(max_length=settings.NOTE_MAX_FILES)] = []
    expires_at: ExpiresAt | None = None
    burn_after_read: bool = False
    password: Password | None = None

    @model_validator(mode="after")
    def text_or_files(self) -> Self:
        if not self.text and not self.file_ids:
            raise ValueError("Note needs text or files")
        if len(set(self.file_ids)) != len(self.file_ids):
            raise ValueError("file_ids must be unique")
        return self


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


class NotePassword(BaseModel):
    password: Password


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    # None у непрочитанной одноразовой заметки и у заметки с паролем:
    # текст отдают только POST /{slug}/read и POST /{slug}/unlock
    text: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    burn_after_read: bool
    has_password: bool
    files: list[LinkedFileRead]
