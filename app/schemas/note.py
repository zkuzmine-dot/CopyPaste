from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.schemas.file import FileRead
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


class NoteCreate(BaseModel):
    text: Annotated[str, Field(max_length=NOTE_TEXT_MAX_LENGTH)] = ""
    file_ids: Annotated[list[FileId], Field(max_length=settings.NOTE_MAX_FILES)] = []
    expires_at: ExpiresAt | None = None
    burn_after_read: bool = False

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


class NoteFileRead(FileRead):
    # None, пока ссылки выдавать нельзя — у непрочитанной одноразовой заметки
    download_url: str | None
    # Только у картинок и не у одноразовых заметок
    preview_url: str | None


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    # None у непрочитанной одноразовой заметки: текст отдаёт только POST /{slug}/read
    text: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    burn_after_read: bool
    files: list[NoteFileRead]
