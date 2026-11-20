import re

# Только один диапазон: несколько через запятую сервер вправе не поддерживать (RFC 9110, 14.2)
_SINGLE_RANGE = re.compile(r"bytes=(\d*)-(\d*)", re.ASCII)


class RangeNotSatisfiableError(Exception):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Диапазон (start, end) включительно, или None — отдать файл целиком.

    Непонятный заголовок не ошибка — его просто не учитываем. RangeNotSatisfiableError,
    если диапазон начинается за концом файла: ответ 416.
    """
    match = _SINGLE_RANGE.fullmatch(header.strip()) if header else None
    if match is None:
        return None
    first, last = match.groups()
    if first:
        start = int(first)
        if last and int(last) < start:
            return None
        end = int(last) if last else size - 1
    elif last:
        # bytes=-500 — последние 500 байт
        suffix = int(last)
        if suffix == 0:
            raise RangeNotSatisfiableError
        start, end = max(size - suffix, 0), size - 1
    else:
        return None
    if start >= size:
        raise RangeNotSatisfiableError
    return start, min(end, size - 1)
