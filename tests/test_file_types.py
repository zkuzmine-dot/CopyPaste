from urllib.parse import quote

import pytest

from app.utils.file_types import (
    MAX_NAME_LENGTH,
    OCTET_STREAM,
    clean_filename,
    content_disposition,
    detect_content_type,
    is_previewable,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        (b"GIF87a" + b"\x00" * 6, "image/gif"),
        (b"GIF89a" + b"\x00" * 6, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
        (b"RIFF\x00\x00\x00\x00WAVE", OCTET_STREAM),
        (b"<html><script>", OCTET_STREAM),
        (b"<svg xmlns='http", OCTET_STREAM),
        (b"%PDF-1.7", OCTET_STREAM),
        (b"\x89PN", OCTET_STREAM),
        (b"", OCTET_STREAM),
    ],
    ids=["png", "jpeg", "gif87", "gif89", "webp", "wav", "html", "svg", "pdf", "cut_png", "empty"],
)
def test_detect_content_type(head, expected):
    assert detect_content_type(head) == expected


def test_only_raster_images_are_previewable():
    assert is_previewable("image/png")
    assert is_previewable("image/webp")
    assert not is_previewable("image/svg+xml")
    assert not is_previewable(OCTET_STREAM)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("Отчёт за май.pdf", "Отчёт за май.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\photo.jpg", "photo.jpg"),
        ("evil\r\nSet-Cookie: a.txt", "evilSet-Cookie: a.txt"),
        ("photo\u202egpj.exe", "photogpj.exe"),
        ("  name.txt  ", "name.txt"),
        ("file.", "file"),
        ("..", "file"),
        ("/", "file"),
        ("", "file"),
    ],
    ids=[
        "plain",
        "cyrillic",
        "unix_path",
        "windows_path",
        "newlines",
        "rtl_override",
        "spaces",
        "trailing_dot",
        "dots",
        "slash",
        "empty",
    ],
)
def test_clean_filename(name, expected):
    assert clean_filename(name) == expected


def test_clean_filename_normalizes_unicode():
    # «й» из двух символов (и + кратка) и из одного должны давать одно имя
    assert clean_filename("и\u0306.txt") == "\u0439.txt"


def test_long_name_keeps_extension():
    name = clean_filename("а" * 300 + ".pdf")

    assert len(name) == MAX_NAME_LENGTH
    assert name.endswith(".pdf")


def test_long_name_without_extension_is_cut():
    assert clean_filename("a" * 300) == "a" * MAX_NAME_LENGTH


def test_content_disposition_ascii():
    assert content_disposition("report.pdf", inline=False) == (
        "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
    )


def test_content_disposition_unicode_and_quotes():
    name = 'Отчёт "май".pdf'

    header = content_disposition(name, inline=True)

    assert header == (
        f"inline; filename=\"_____ _____.pdf\"; filename*=UTF-8''{quote(name, safe='')}"
    )
