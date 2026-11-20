from fastapi import Request


def client_ip(request: Request) -> str:
    # За обратным прокси здесь его адрес, если uvicorn не запущен с --proxy-headers.
    # X-Forwarded-For сами не читаем: его может прислать кто угодно и так обойти лимиты
    return request.client.host if request.client else "unknown"
