from datetime import timedelta

import pytest
from sqlalchemy import func, update

from app.db.models.note import Note
from app.utils.qr import make_qr_svg

URL = "/api/notes"

pytestmark = pytest.mark.anyio


@pytest.fixture
async def slug(async_client) -> str:
    return (await async_client.post(URL, json={"text": "hello"})).json()["slug"]


def test_make_qr_svg():
    svg = make_qr_svg("http://example.com/n/abc")

    assert svg.startswith("<svg")
    # Без xmlns браузер не покажет SVG как отдельную картинку в <img>
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert svg.rstrip().endswith("</svg>")


async def test_note_qr(async_client, slug):
    response = await async_client.get(f"{URL}/{slug}/qr")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    # QR детерминирован: тот же текст даёт тот же SVG — так проверяем, что внутри верная ссылка
    assert response.text == make_qr_svg(f"http://test/n/{slug}")


async def test_note_qr_unknown_slug(async_client):
    response = await async_client.get(f"{URL}/zzzzzzzz/qr")

    assert response.status_code == 404


async def test_note_qr_expired(async_client, connection, slug):
    connection.execute(
        update(Note).where(Note.slug == slug).values(expires_at=func.now() - timedelta(seconds=1))
    )

    response = await async_client.get(f"{URL}/{slug}/qr")

    assert response.status_code == 404
