import anyio
import pytest
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from app.storage.responses import CompletionTrackingFileResponse

CONTENT = b"0123456789" * 3

# Ответ проверяем напрямую через ASGI: так видно, что происходит при обрыве,
# чего через TestClient не воспроизвести


@pytest.fixture
def file_path(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(CONTENT)
    return path


def http_scope(method: str = "GET", headers=()):
    return {"type": "http", "method": method, "path": "/", "headers": list(headers)}


async def quiet_receive():
    # Клиент ничего не присылает и не отключается
    await anyio.sleep_forever()


class Client:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, message) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        return self.messages[0]["status"]

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )


def disconnect_after_first_chunk():
    first_chunk = anyio.Event()

    async def send(message) -> None:
        if message["type"] == "http.response.body":
            first_chunk.set()
            await anyio.sleep_forever()  # клиент перестал читать

    async def receive():
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    return send, receive


def tracked(path, calls: list[str]) -> CompletionTrackingFileResponse:
    async def on_complete() -> None:
        calls.append("complete")

    response = CompletionTrackingFileResponse(path, on_complete=on_complete)
    response.chunk_size = 4
    return response


@pytest.mark.anyio
async def test_full_download_calls_on_complete_once(file_path):
    calls: list[str] = []
    client = Client()

    await tracked(file_path, calls)(http_scope(), quiet_receive, client.send)

    assert client.body == CONTENT
    assert calls == ["complete"]


@pytest.mark.anyio
async def test_interrupted_download_does_not_call_on_complete(file_path):
    calls: list[str] = []
    send, receive = disconnect_after_first_chunk()

    await tracked(file_path, calls)(http_scope(), receive, send)

    assert calls == []


@pytest.mark.anyio
async def test_starlette_background_task_runs_even_after_disconnect(file_path):
    # Из-за этого поведения и нужен свой ответ. Если Starlette его поменяет,
    # тест подскажет, что CompletionTrackingFileResponse можно упростить
    calls: list[str] = []

    async def record() -> None:
        calls.append("background")

    response = FileResponse(file_path, background=BackgroundTask(record))
    response.chunk_size = 4
    send, receive = disconnect_after_first_chunk()

    await response(http_scope(), receive, send)

    assert calls == ["background"]


@pytest.mark.anyio
async def test_head_request_does_not_call_on_complete(file_path):
    calls: list[str] = []
    client = Client()

    await tracked(file_path, calls)(http_scope("HEAD"), quiet_receive, client.send)

    assert client.body == b""
    assert calls == []


@pytest.mark.anyio
async def test_range_is_ignored(file_path):
    calls: list[str] = []
    client = Client()
    scope = http_scope(headers=[(b"range", b"bytes=0-3")])

    await tracked(file_path, calls)(scope, quiet_receive, client.send)

    assert client.status == 200
    assert client.body == CONTENT
    assert calls == ["complete"]


@pytest.mark.anyio
async def test_send_error_does_not_call_on_complete(file_path):
    # Серверы на ASGI 2.4 сообщают об обрыве исключением из send
    calls: list[str] = []

    async def failing_send(message) -> None:
        if message["type"] == "http.response.body":
            raise OSError("connection reset")

    with pytest.raises(OSError, match="connection reset"):
        await tracked(file_path, calls)(http_scope(), quiet_receive, failing_send)

    assert calls == []
