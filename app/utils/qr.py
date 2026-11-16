import io

import segno


def make_qr_svg(data: str) -> str:
    qr = segno.make(data, error="m")
    buffer = io.BytesIO()
    # Тёмное на белом независимо от темы страницы — иначе сканеры читают хуже
    qr.save(buffer, kind="svg", scale=8, border=2, dark="#000000", light="#ffffff", xmldecl=False)
    return buffer.getvalue().decode()
