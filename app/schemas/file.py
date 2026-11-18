from typing import Self

from pydantic import BaseModel

from app.db.models.file import StoredFile
from app.utils.file_types import is_previewable


class FileRead(BaseModel):
    id: str
    name: str
    size: int
    content_type: str
    is_image: bool

    @classmethod
    def from_stored(cls, stored: StoredFile) -> Self:
        return cls(
            id=stored.public_id,
            name=stored.name,
            size=stored.size_bytes,
            content_type=stored.content_type,
            is_image=is_previewable(stored.content_type),
        )
