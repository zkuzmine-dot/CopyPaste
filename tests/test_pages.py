import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def page_client() -> TestClient:
    # Страницам БД не нужна, поэтому без фикстуры client
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/note", "/n/a5z573fs", "/c/a5z573fs"])
def test_page_returns_html(page_client, path):
    response = page_client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<textarea" in response.text
    assert '<script src="/static/app.js">' in response.text
    assert '<script src="/static/channel.js">' in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/app.js", "javascript"),
        ("/static/channel.js", "javascript"),
        ("/static/style.css", "text/css"),
    ],
)
def test_static_files(page_client, path, content_type):
    response = page_client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_pages_not_in_openapi(page_client):
    paths = page_client.get("/openapi.json").json()["paths"]

    assert "/" not in paths
    assert "/note" not in paths
    assert "/n/{slug}" not in paths
    assert "/c/{slug}" not in paths
