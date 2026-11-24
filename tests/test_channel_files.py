from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models.channel import Message
from app.db.models.file import StoredFile
from app.jobs.cleanup import run_channels_cleanup
from app.jobs.files import run_files_gc
from app.schemas.channel import FILE_META_MAX_BYTES
from app.services import channels as channels_service
from app.settings import settings
from tests.channel_keys import device_keys, exchange_keys_by_token, fake_payload_b64, join_by_http

pytestmark = pytest.mark.anyio

URL = "/api/channels"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def client(committing_client):
    # Как в test_channels_ws: настоящие коммиты — обработчик и таймер ходят в БД одновременно
    return committing_client


@pytest.fixture
def factory(committing_session_factory):
    return committing_session_factory


@pytest.fixture
async def channel(client, factory) -> dict:
    keys = device_keys()
    created = (await client.post(URL)).json()
    joined = (await join_by_http(client, created["slug"], keys)).json()
    async with factory() as db, db.begin():
        await exchange_keys_by_token(db, created["slug"], joined["token"], keys)
    return {"slug": created["slug"], "receiver": created["token"], "sender": joined["token"]}


# Описание файла зашифровано: серверу всё равно, что внутри
META = fake_payload_b64("описание файла")


async def upload(client, slug: str, token: str | None, content=b"hello"):
    headers = {"X-Participant-Token": token} if token else {}
    return await client.post(f"{URL}/{slug}/files", content=content, headers=headers)


async def uploaded(client, channel: dict, content=b"hello") -> str:
    response = await upload(client, channel["slug"], channel["sender"], content)
    assert response.status_code == 201
    return response.json()["id"]


def connect(client, slug: str):
    return client.websocket(f"/ws/channels/{slug}")


async def auth(ws, token: str) -> dict:
    await ws.send_json({"type": "auth", "token": token})
    return await ws.receive_json()


@asynccontextmanager
async def both_connected(client, channel: dict):
    async with (
        connect(client, channel["slug"]) as receiver,
        connect(client, channel["slug"]) as sender,
    ):
        await auth(receiver, channel["receiver"])
        await auth(sender, channel["sender"])
        assert (await receiver.receive_json())["type"] == "peer_status"
        yield receiver, sender


async def send_file(ws, file_id: str) -> None:
    await ws.send_json({"type": "send_file", "file_id": file_id, "payload": META})


async def swap_roles(receiver, sender) -> None:
    await receiver.send_json({"type": "become_sender"})
    assert (await receiver.receive_json())["role"] == "sender"
    assert (await sender.receive_json())["role"] == "receiver"


async def row(factory, file_id: str) -> StoredFile | None:
    async with factory() as db:
        return await db.scalar(select(StoredFile).where(StoredFile.public_id == file_id))


def files_on_disk(file_storage) -> list:
    root = file_storage.root
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# --- загрузка ---


async def test_sender_uploads_ciphertext_as_is(client, channel, factory):
    # Даже байты PNG: в канал приходит шифротекст, тип по нему не узнать, превью нет
    response = await upload(client, channel["slug"], channel["sender"], PNG)

    assert response.status_code == 201
    assert (response.json()["name"], response.json()["is_image"]) == ("file", False)
    stored = await row(factory, response.json()["id"])
    assert stored.content_type == "application/octet-stream"
    assert stored.origin == "channel"
    assert stored.uploaded_by_id is not None
    assert stored.message_id is None


async def test_upload_is_rejected_before_reading_body(client, channel, file_storage):
    other = (await client.post(URL)).json()
    slug = channel["slug"]

    assert (await upload(client, slug, None)).status_code == 401
    assert (await upload(client, slug, "wrong-token")).status_code == 401
    assert (await upload(client, slug, other["token"])).status_code == 401
    assert (await upload(client, slug, channel["receiver"])).status_code == 403
    assert (await upload(client, "zzzzzzzz", channel["sender"])).status_code == 404
    assert files_on_disk(file_storage) == []


async def test_upload_limit_counts_chunk_tags(client, channel, monkeypatch):
    # 100 байт файла — один кусок, к нему 16 байт тега
    monkeypatch.setattr(settings, "FILE_MAX_SIZE_BYTES", 100)
    slug, token = channel["slug"], channel["sender"]

    assert (await upload(client, slug, token, b"x" * 116)).status_code == 201
    assert (await upload(client, slug, token, b"x" * 117)).status_code == 413


async def test_upload_before_key_exchange_is_rejected(client, file_storage):
    created = (await client.post(URL)).json()
    joined = (await join_by_http(client, created["slug"])).json()

    response = await upload(client, created["slug"], joined["token"])

    assert response.status_code == 409
    assert response.json() == {"detail": "Keys are not exchanged yet"}
    assert files_on_disk(file_storage) == []


# --- отправка ---


async def test_send_file_reaches_both_with_links_only_for_receiver(client, channel):
    file_id = await uploaded(client, channel, PNG)

    async with both_connected(client, channel) as (receiver, sender):
        await send_file(sender, file_id)
        own, theirs = await sender.receive_json(), await receiver.receive_json()

    for event in (own, theirs):
        assert (event["type"], event["kind"], event["payload"]) == ("message", "file", META)
        assert (event["file"]["id"], event["file"]["name"]) == (file_id, "file")
    own_links = (
        own["file"]["download_url"],
        own["file"]["preview_url"],
        own["file"]["received_url"],
    )
    assert own_links == (None, None, None)
    assert theirs["file"]["download_url"] is not None
    assert theirs["file"]["preview_url"] is None
    assert theirs["file"]["received_url"] is not None
    lifetime = datetime.fromisoformat(theirs["expires_at"]) - datetime.fromisoformat(
        theirs["created_at"]
    )
    assert lifetime == timedelta(seconds=settings.CHANNEL_FILE_TTL_SECONDS)


async def test_received_removes_file_for_both(client, channel, factory, file_storage):
    file_id = await uploaded(client, channel, b"payload")
    path = file_storage.path((await row(factory, file_id)).storage_key)

    async with both_connected(client, channel) as (receiver, sender):
        await send_file(sender, file_id)
        await sender.receive_json()
        message = await receiver.receive_json()
        links = message["file"]

        # Скачивание само ничего не удаляет: оборванное на медленной сети можно повторить
        assert (await client.get(links["download_url"])).content == b"payload"
        assert await row(factory, file_id) is not None

        assert (await client.post(links["received_url"])).status_code == 204
        for ws in (receiver, sender):
            assert await ws.receive_json() == {
                "type": "file_downloaded",
                "message_id": message["id"],
            }
        assert (await client.get(links["download_url"])).status_code == 410

    assert await row(factory, file_id) is None
    assert not path.exists()
    async with factory() as db:
        assert await db.get(Message, message["id"]) is None


# --- отказы ---


@pytest.mark.parametrize(
    "payload",
    [None, fake_payload_b64("x" * (FILE_META_MAX_BYTES + 1))],
    ids=["no_description", "description_too_long"],
)
async def test_send_file_requires_description(client, channel, factory, payload):
    file_id = await uploaded(client, channel)
    event = {"type": "send_file", "file_id": file_id, "payload": payload}

    async with both_connected(client, channel) as (_receiver, sender):
        await sender.send_json({k: v for k, v in event.items() if v is not None})
        error = await sender.receive_json()

    assert error["code"] == "invalid_event"
    assert (await row(factory, file_id)).message_id is None


async def test_former_sender_cannot_send_file(client, channel, factory):
    file_id = await uploaded(client, channel)

    async with both_connected(client, channel) as (receiver, sender):
        await swap_roles(receiver, sender)
        # Загрузил, пока был отправителем, но отправить уже не может
        await send_file(sender, file_id)
        assert (await sender.receive_json())["code"] == "not_sender"

    assert (await row(factory, file_id)).message_id is None


async def test_new_sender_cannot_send_peers_upload(client, channel):
    file_id = await uploaded(client, channel)

    async with both_connected(client, channel) as (receiver, sender):
        await swap_roles(receiver, sender)
        await send_file(receiver, file_id)
        assert (await receiver.receive_json())["code"] == "file_not_found"


async def test_send_file_rejects_unknown_and_note_files(client, channel):
    response = await client.post("/api/files", params={"name": "a.txt"}, content=b"x")
    note_file = response.json()["id"]

    async with both_connected(client, channel) as (_receiver, sender):
        for file_id in ("never-uploaded", note_file):
            await send_file(sender, file_id)
            assert (await sender.receive_json())["code"] == "file_not_found"


async def test_same_file_cannot_be_sent_twice(client, channel):
    file_id = await uploaded(client, channel)

    async with both_connected(client, channel) as (receiver, sender):
        await send_file(sender, file_id)
        await sender.receive_json()
        await receiver.receive_json()

        await send_file(sender, file_id)
        assert (await sender.receive_json())["code"] == "file_not_found"


# --- состояние ---


async def test_state_has_links_only_for_receiver(client, channel):
    file_id = await uploaded(client, channel)
    async with both_connected(client, channel) as (receiver, sender):
        await send_file(sender, file_id)
        await sender.receive_json()
        await receiver.receive_json()

    for token, has_link in ((channel["receiver"], True), (channel["sender"], False)):
        async with connect(client, channel["slug"]) as ws:
            [ws_message] = (await auth(ws, token))["messages"]
        headers = {"X-Participant-Token": token}
        state = (await client.get(f"{URL}/{channel['slug']}", headers=headers)).json()
        [rest_message] = state["messages"]
        for message in (ws_message, rest_message):
            assert message["kind"] == "file"
            assert (message["file"]["download_url"] is not None) is has_link


# --- срок жизни ---


async def send_and_get_link(client, channel) -> str:
    async with both_connected(client, channel) as (receiver, sender):
        await send_file(sender, await uploaded(client, channel))
        await sender.receive_json()
        return (await receiver.receive_json())["file"]["download_url"]


async def test_expired_file_message_is_collected(client, channel, factory, file_storage):
    link = await send_and_get_link(client, channel)
    async with factory() as db, db.begin():
        await db.execute(update(Message).values(expires_at=func.now() - timedelta(seconds=1)))

    # Чистка ещё не прошла, а файл уже не отдаётся
    assert (await client.get(link)).status_code == 410
    assert await run_channels_cleanup(factory) == (1, 0)
    assert await run_files_gc(factory, file_storage) == 1
    assert files_on_disk(file_storage) == []


async def test_deleted_channel_files_are_collected(client, channel, factory, file_storage):
    link = await send_and_get_link(client, channel)
    async with factory() as db, db.begin():
        active = await channels_service.get_active_channel(db, channel["slug"])
        await channels_service.close_channel(db, active)

    assert await run_channels_cleanup(factory) == (0, 1)
    assert await run_files_gc(factory, file_storage) == 1
    assert files_on_disk(file_storage) == []
    assert (await client.get(link)).status_code == 410
