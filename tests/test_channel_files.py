from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import sessionmaker

from app.api.files.routes import create_upload_rate_limiter
from app.db.models.channel import Channel, Message
from app.db.models.file import StoredFile
from app.jobs.cleanup import run_channels_cleanup
from app.jobs.files import run_files_gc
from app.main import app
from app.services import channels as channels_service
from app.settings import settings

URL = "/api/channels"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def client(engine, factory, file_storage, redis_client):
    # Как в test_channels_ws: настоящие коммиты и один event loop на все соединения.
    # Хранилище — во временной папке, а не в томе приложения
    with TestClient(app) as client:
        app.state.session_factory = factory
        app.state.file_storage = file_storage
        app.state.upload_rate_limiter = create_upload_rate_limiter(redis_client)
        yield client
    with engine.begin() as conn:
        conn.execute(delete(Channel))
        conn.execute(delete(StoredFile))


@pytest.fixture
def channel(client) -> dict:
    created = client.post(URL).json()
    joined = client.post(f"{URL}/{created['slug']}/join").json()
    return {"slug": created["slug"], "receiver": created["token"], "sender": joined["token"]}


def upload(client, slug: str, token: str | None, content=b"hello", name="hello.txt"):
    headers = {"X-Participant-Token": token} if token else {}
    return client.post(
        f"{URL}/{slug}/files", params={"name": name}, content=content, headers=headers
    )


def uploaded(client, channel: dict, content=b"hello", name="hello.txt") -> str:
    response = upload(client, channel["slug"], channel["sender"], content, name)
    assert response.status_code == 201
    return response.json()["id"]


def connect(client, slug: str):
    return client.websocket_connect(f"/ws/channels/{slug}")


def auth(ws, token: str) -> dict:
    ws.send_json({"type": "auth", "token": token})
    return ws.receive_json()


@contextmanager
def both_connected(client, channel: dict):
    with connect(client, channel["slug"]) as receiver, connect(client, channel["slug"]) as sender:
        auth(receiver, channel["receiver"])
        auth(sender, channel["sender"])
        assert receiver.receive_json()["type"] == "peer_status"
        yield receiver, sender


def send_file(ws, file_id: str) -> None:
    ws.send_json({"type": "send_file", "file_id": file_id})


def swap_roles(receiver, sender) -> None:
    receiver.send_json({"type": "become_sender"})
    assert receiver.receive_json()["role"] == "sender"
    assert sender.receive_json()["role"] == "receiver"


def row(factory, file_id: str) -> StoredFile | None:
    with factory() as db:
        return db.scalar(select(StoredFile).where(StoredFile.public_id == file_id))


def files_on_disk(file_storage) -> list:
    root = file_storage.root
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# --- загрузка ---


def test_sender_uploads(client, channel, factory):
    response = upload(client, channel["slug"], channel["sender"], PNG, "photo.png")

    assert response.status_code == 201
    assert response.json()["is_image"] is True
    stored = row(factory, response.json()["id"])
    assert stored.origin == "channel"
    assert stored.uploaded_by_id is not None
    assert stored.message_id is None


def test_upload_is_rejected_before_reading_body(client, channel, file_storage):
    other = client.post(URL).json()
    slug = channel["slug"]

    assert upload(client, slug, None).status_code == 401
    assert upload(client, slug, "wrong-token").status_code == 401
    assert upload(client, slug, other["token"]).status_code == 401
    assert upload(client, slug, channel["receiver"]).status_code == 403
    assert upload(client, "zzzzzzzz", channel["sender"]).status_code == 404
    assert files_on_disk(file_storage) == []


# --- отправка ---


def test_send_file_reaches_both_with_links_only_for_receiver(client, channel):
    file_id = uploaded(client, channel, PNG, "photo.png")

    with both_connected(client, channel) as (receiver, sender):
        send_file(sender, file_id)
        own, theirs = sender.receive_json(), receiver.receive_json()

    for event in (own, theirs):
        assert (event["type"], event["kind"], event["text"]) == ("message", "file", None)
        assert (event["file"]["id"], event["file"]["name"]) == (file_id, "photo.png")
    own_links = (
        own["file"]["download_url"],
        own["file"]["preview_url"],
        own["file"]["received_url"],
    )
    assert own_links == (None, None, None)
    assert theirs["file"]["download_url"] is not None
    assert theirs["file"]["preview_url"] is not None
    assert theirs["file"]["received_url"] is not None
    lifetime = datetime.fromisoformat(theirs["expires_at"]) - datetime.fromisoformat(
        theirs["created_at"]
    )
    assert lifetime == timedelta(seconds=settings.CHANNEL_FILE_TTL_SECONDS)


def test_received_removes_file_for_both(client, channel, factory, file_storage):
    file_id = uploaded(client, channel, b"payload", "data.bin")
    path = file_storage.path(row(factory, file_id).storage_key)

    with both_connected(client, channel) as (receiver, sender):
        send_file(sender, file_id)
        sender.receive_json()
        message = receiver.receive_json()
        links = message["file"]

        # Скачивание само ничего не удаляет: оборванное на медленной сети можно повторить
        assert client.get(links["download_url"]).content == b"payload"
        assert row(factory, file_id) is not None

        assert client.post(links["received_url"]).status_code == 204
        for ws in (receiver, sender):
            assert ws.receive_json() == {"type": "file_downloaded", "message_id": message["id"]}
        assert client.get(links["download_url"]).status_code == 410

    assert row(factory, file_id) is None
    assert not path.exists()
    with factory() as db:
        assert db.get(Message, message["id"]) is None


def test_preview_does_not_remove_file(client, channel, factory):
    file_id = uploaded(client, channel, PNG, "photo.png")

    with both_connected(client, channel) as (receiver, sender):
        send_file(sender, file_id)
        sender.receive_json()
        message = receiver.receive_json()

        preview = client.get(message["file"]["preview_url"])

    assert preview.headers["content-type"] == "image/png"
    assert row(factory, file_id) is not None


# --- отказы ---


def test_former_sender_cannot_send_file(client, channel, factory):
    file_id = uploaded(client, channel)

    with both_connected(client, channel) as (receiver, sender):
        swap_roles(receiver, sender)
        # Загрузил, пока был отправителем, но отправить уже не может
        send_file(sender, file_id)
        assert sender.receive_json()["code"] == "not_sender"

    assert row(factory, file_id).message_id is None


def test_new_sender_cannot_send_peers_upload(client, channel):
    file_id = uploaded(client, channel)

    with both_connected(client, channel) as (receiver, sender):
        swap_roles(receiver, sender)
        send_file(receiver, file_id)
        assert receiver.receive_json()["code"] == "file_not_found"


def test_send_file_rejects_unknown_and_note_files(client, channel):
    note_file = client.post("/api/files", params={"name": "a.txt"}, content=b"x").json()["id"]

    with both_connected(client, channel) as (_receiver, sender):
        for file_id in ("never-uploaded", note_file):
            send_file(sender, file_id)
            assert sender.receive_json()["code"] == "file_not_found"


def test_same_file_cannot_be_sent_twice(client, channel):
    file_id = uploaded(client, channel)

    with both_connected(client, channel) as (receiver, sender):
        send_file(sender, file_id)
        sender.receive_json()
        receiver.receive_json()

        send_file(sender, file_id)
        assert sender.receive_json()["code"] == "file_not_found"


# --- состояние ---


def test_state_has_links_only_for_receiver(client, channel):
    file_id = uploaded(client, channel)
    with both_connected(client, channel) as (receiver, sender):
        send_file(sender, file_id)
        sender.receive_json()
        receiver.receive_json()

    for token, has_link in ((channel["receiver"], True), (channel["sender"], False)):
        with connect(client, channel["slug"]) as ws:
            [ws_message] = auth(ws, token)["messages"]
        headers = {"X-Participant-Token": token}
        [rest_message] = client.get(f"{URL}/{channel['slug']}", headers=headers).json()["messages"]
        for message in (ws_message, rest_message):
            assert message["kind"] == "file"
            assert (message["file"]["download_url"] is not None) is has_link


# --- срок жизни ---


def send_and_get_link(client, channel) -> str:
    with both_connected(client, channel) as (receiver, sender):
        send_file(sender, uploaded(client, channel))
        sender.receive_json()
        return receiver.receive_json()["file"]["download_url"]


def test_expired_file_message_is_collected(client, channel, factory, file_storage):
    link = send_and_get_link(client, channel)
    with factory.begin() as db:
        db.execute(update(Message).values(expires_at=func.now() - timedelta(seconds=1)))

    # Чистка ещё не прошла, а файл уже не отдаётся
    assert client.get(link).status_code == 410
    assert run_channels_cleanup(factory) == (1, 0)
    assert run_files_gc(factory, file_storage) == 1
    assert files_on_disk(file_storage) == []


def test_deleted_channel_files_are_collected(client, channel, factory, file_storage):
    link = send_and_get_link(client, channel)
    with factory.begin() as db:
        channels_service.close_channel(db, channels_service.get_active_channel(db, channel["slug"]))

    assert run_channels_cleanup(factory) == (0, 1)
    assert run_files_gc(factory, file_storage) == 1
    assert files_on_disk(file_storage) == []
    assert client.get(link).status_code == 410
