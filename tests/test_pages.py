import pytest

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("path", ["/", "/note", "/n/a5z573fs", "/c/a5z573fs"])
async def test_page_returns_html(client, path):
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<textarea" in response.text
    assert '<script src="/static/app.js">' in response.text
    assert '<script src="/static/channel.js">' in response.text
    assert '<script src="/static/files.js">' in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/app.js", "javascript"),
        ("/static/channel.js", "javascript"),
        ("/static/files.js", "javascript"),
        ("/static/style.css", "text/css"),
    ],
)
async def test_static_files(client, path, content_type):
    response = await client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


async def test_pages_not_in_openapi(client):
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert "/" not in paths
    assert "/note" not in paths
    assert "/n/{slug}" not in paths
    assert "/c/{slug}" not in paths
