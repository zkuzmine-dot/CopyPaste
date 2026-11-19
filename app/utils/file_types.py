import re
import unicodedata
from urllib.parse import quote

OCTET_STREAM = "application/octet-stream"
# Сколько первых байт нужно, чтобы узнать любую из картинок ниже
SNIFF_BYTES = 12
MAX_NAME_LENGTH = 255
_MAX_EXTENSION_LENGTH = 16

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
PREVIEWABLE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def detect_content_type(head: bytes) -> str:
    """Тип по первым байтам. Заголовку Content-Type от клиента не верим."""
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            return content_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return OCTET_STREAM


def is_previewable(content_type: str) -> bool:
    return content_type in PREVIEWABLE_TYPES


def clean_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = re.split(r"[/\\]", name)[-1]
    # Cc — управляющие символы (переводы строк), Cf — невидимые, например U+202E:
    # он разворачивает текст, и «gpj.exe» выглядит как «exe.jpg»
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf"))
    name = name.strip().rstrip(". ")
    if len(name) > MAX_NAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        if dot and stem and len(extension) <= _MAX_EXTENSION_LENGTH:
            name = f"{stem[: MAX_NAME_LENGTH - len(extension) - 1]}.{extension}"
        else:
            name = name[:MAX_NAME_LENGTH]
    return name or "file"


def content_disposition(filename: str, *, inline: bool) -> str:
    # filename — запасной вариант для старых клиентов (только ASCII),
    # filename* — настоящее имя в UTF-8 (RFC 6266)
    fallback = filename.encode("ascii", "replace").decode()
    fallback = re.sub(r'[?"\\]', "_", fallback)
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
