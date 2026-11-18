from collections.abc import Awaitable, Callable
from os import PathLike
from typing import Any

from starlette.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send

OnComplete = Callable[[], Awaitable[None]]


class CompletionTrackingFileResponse(FileResponse):
    """Вызывает on_complete, только если файл отдан целиком.

    BackgroundTask для этого не годится: при обрыве соединения Starlette отменяет передачу,
    но фоновую задачу всё равно запускает. Поэтому считаем отданные байты сами.
    """

    def __init__(self, path: str | PathLike[str], *, on_complete: OnComplete, **kwargs: Any):
        super().__init__(path, **kwargs)
        self._on_complete = on_complete

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Без Range: докачанный кусок — не скачивание. Без pathsend: тогда байты отдаёт
        # сервер в обход send, и посчитать их нельзя
        extensions = scope.get("extensions") or {}
        scope = {
            **scope,
            "headers": [(k, v) for k, v in scope["headers"] if k not in (b"range", b"if-range")],
            "extensions": {k: v for k, v in extensions.items() if k != "http.response.pathsend"},
        }
        sent = 0

        async def counting_send(message: Message) -> None:
            nonlocal sent
            if message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
            await send(message)

        await super().__call__(scope, receive, counting_send)
        if scope["method"] == "GET" and sent == int(self.headers["content-length"]):
            await self._on_complete()
