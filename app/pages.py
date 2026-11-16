from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

router = APIRouter(include_in_schema=False)


@router.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


# Заметку загружает JS по slug из адреса, поэтому сервер отдаёт ту же страницу
@router.get("/n/{slug}")
def note_page(slug: str) -> FileResponse:
    return FileResponse(INDEX_HTML)


@router.get("/c/{slug}")
def channel_page(slug: str) -> FileResponse:
    return FileResponse(INDEX_HTML)
