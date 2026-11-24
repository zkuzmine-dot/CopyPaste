import re

import pytest

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("path", ["/", "/note", "/about", "/n/a5z573fs", "/c/a5z573fs"])
async def test_page_returns_html(client, path):
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<textarea" in response.text
    assert '<script src="/static/app.js">' in response.text
    assert '<script src="/static/channel.js">' in response.text
    assert '<script src="/static/files.js">' in response.text


async def test_theme_is_set_before_styles(client):
    # Иначе страница сначала отрисуется в системной теме и мигнёт при переключении
    html = (await client.get("/")).text
    head = html[: html.index("</head>")]

    assert head.index('<script src="/static/prefs.js">') < head.index('href="/static/style.css"')


async def test_translation_loads_before_code_that_uses_it(client):
    html = (await client.get("/")).text
    order = ["i18n-en.js", "i18n.js", "files.js", "channel.js", "app.js"]
    positions = [html.index(f'<script src="/static/{name}">') for name in order]

    assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/prefs.js", "javascript"),
        ("/static/i18n.js", "javascript"),
        ("/static/i18n-en.js", "javascript"),
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


async def test_fonts_in_stylesheet_are_served(client):
    # Шрифт по неверному пути не ломает страницу: браузер молча берёт системный
    css = (await client.get("/static/style.css")).text
    urls = re.findall(r'url\("([^"]+)"\)', css)

    assert urls and len(urls) == css.count("@font-face")
    for url in urls:
        response = await client.get(f"/static/{url}")
        assert response.status_code == 200, url
        assert response.content[:4] == b"wOF2", url


async def test_pages_not_in_openapi(client):
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert "/" not in paths
    assert "/note" not in paths
    assert "/about" not in paths
    assert "/n/{slug}" not in paths
    assert "/c/{slug}" not in paths
