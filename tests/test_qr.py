from datetime import timedelta

import pytest
from sqlalchemy import func, update

from app.db.models.note import Note
from app.utils.qr import make_qr_svg

URL = "/api/notes"


@pytest.fixture
def slug(client) -> str:
    return client.post(URL, json={"text": "hello"}).json()["slug"]


def test_make_qr_svg():
    svg = make_qr_svg("http://example.com/n/abc")

    assert svg.startswith("<svg")
    # Без xmlns браузер не покажет SVG как отдельную картинку в <img>
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert svg.rstrip().endswith("</svg>")


def test_note_qr(client, slug):
    response = client.get(f"{URL}/{slug}/qr")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    # QR детерминирован: тот же текст даёт тот же SVG — так проверяем, что внутри верная ссылка
    assert response.text == make_qr_svg(f"http://testserver/n/{slug}")


def test_note_qr_unknown_slug(client):
    response = client.get(f"{URL}/zzzzzzzz/qr")

    assert response.status_code == 404


def test_note_qr_expired(client, connection, slug):
    connection.execute(
        update(Note).where(Note.slug == slug).values(expires_at=func.now() - timedelta(seconds=1))
    )

    response = client.get(f"{URL}/{slug}/qr")

    assert response.status_code == 404
