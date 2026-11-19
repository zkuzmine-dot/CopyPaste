from typing import Self

from pydantic import BaseModel

from app.db.models.file import StoredFile
from app.utils import signing
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


class LinkedFileRead(FileRead):
    # None, пока ссылку выдавать нельзя: у непрочитанной одноразовой заметки и у своего
    # файла в канале (иначе отправитель подтверждением удалил бы файл у получателя)
    download_url: str | None
    # Только у картинок
    preview_url: str | None
    # Только у одноразовых файлов: POST сюда, когда файл скачан целиком, — и он удалится
    received_url: str | None

    @classmethod
    def build(cls, stored: StoredFile, *, links_expire: int | None, preview: bool = True) -> Self:
        info = FileRead.from_stored(stored).model_dump()
        if links_expire is None:
            return cls(**info, download_url=None, preview_url=None, received_url=None)
        preview_url = None
        if preview and info["is_image"]:
            preview_url = signing.file_url(stored.public_id, links_expire, inline=True)
        received_url = None
        if stored.delete_after_download:
            received_url = signing.received_url(stored.public_id, links_expire)
        return cls(
            **info,
            download_url=signing.file_url(stored.public_id, links_expire),
            preview_url=preview_url,
            received_url=received_url,
        )
