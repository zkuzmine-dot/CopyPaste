import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from app.api.channels import ws as channels_ws
from app.db.models.channel import Channel, Message
from app.main import app
from app.schemas.note import NOTE_TEXT_MAX_LENGTH
from app.settings import settings

URL = "/api/channels"


@pytest.fixture
def client(engine):
    # Обработчик ходит в БД из потоков параллельно с таймером бездействия, а тестовое
    # подключение из conftest одно на всех — поэтому здесь настоящие коммиты.
    # with: один event loop на все соединения теста, как в uvicorn
    with TestClient(app) as client:
        app.state.session_factory = sessionmaker(bind=engine)
        yield client
    with engine.begin() as conn:
        conn.execute(delete(Channel))


@pytest.fixture
def channel(client) -> dict:
    created = client.post(URL).json()
    joined = client.post(f"{URL}/{created['slug']}/join").json()
    return {"slug": created["slug"], "receiver": created["token"], "sender": joined["token"]}


def connect(client, slug: str):
    return client.websocket_connect(f"/ws/channels/{slug}")


def auth(ws, token: str) -> dict:
    ws.send_json({"type": "auth", "token": token})
    return ws.receive_json()


def expect_close(ws, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    assert exc.value.code == code


def count_messages(engine) -> int:
    with engine.connect() as conn:
        return conn.scalar(select(func.count()).select_from(Message))


# --- аутентификация ---


def test_auth_returns_state(client, channel):
    with connect(client, channel["slug"]) as ws:
        state = auth(ws, channel["receiver"])

    assert state == {
        "type": "state",
        "role": "receiver",
        "peer_joined": True,
        "peer_online": False,
        "messages": [],
    }


def test_state_before_peer_joined(client):
    created = client.post(URL).json()

    with connect(client, created["slug"]) as ws:
        state = auth(ws, created["token"])

    assert state["peer_joined"] is False


@pytest.mark.parametrize(
    "first_message",
    [
        {"type": "auth", "token": "wrong"},
        {"type": "send", "text": "hi"},
        {"type": "auth"},
        "not json",
    ],
    ids=["wrong_token", "not_auth", "no_token", "not_json"],
)
def test_bad_auth_closes_4401(client, channel, first_message):
    with connect(client, channel["slug"]) as ws:
        if isinstance(first_message, str):
            ws.send_text(first_message)
        else:
            ws.send_json(first_message)
        expect_close(ws, 4401)


def test_token_from_other_channel_closes_4401(client, channel):
    other = client.post(URL).json()

    with connect(client, channel["slug"]) as ws:
        ws.send_json({"type": "auth", "token": other["token"]})
        expect_close(ws, 4401)


def test_auth_timeout_closes_4401(client, channel, monkeypatch):
    monkeypatch.setattr(settings, "WS_AUTH_TIMEOUT_SECONDS", 0.1)

    with connect(client, channel["slug"]) as ws:
        expect_close(ws, 4401)


def test_unknown_channel_closes_4404(client, channel):
    with connect(client, "zzzzzzzz") as ws:
        ws.send_json({"type": "auth", "token": channel["receiver"]})
        expect_close(ws, 4404)


def test_closed_channel_closes_4404(client, channel, engine):
    with engine.begin() as conn:
        conn.execute(update(Channel).values(closed_at=func.now()))

    with connect(client, channel["slug"]) as ws:
        ws.send_json({"type": "auth", "token": channel["receiver"]})
        expect_close(ws, 4404)


def test_repeated_auth_is_error(client, channel):
    with connect(client, channel["slug"]) as ws:
        auth(ws, channel["receiver"])
        error = auth(ws, channel["receiver"])

    assert error["type"] == "error"
    assert error["code"] == "already_authenticated"


# --- присутствие ---


def test_peer_status(client, channel):
    with connect(client, channel["slug"]) as receiver:
        auth(receiver, channel["receiver"])

        with connect(client, channel["slug"]) as sender:
            assert auth(sender, channel["sender"])["peer_online"] is True
            assert receiver.receive_json() == {"type": "peer_status", "online": True}

        assert receiver.receive_json() == {"type": "peer_status", "online": False}


def test_same_token_replaces_old_connection(client, channel):
    with connect(client, channel["slug"]) as receiver, connect(client, channel["slug"]) as old:
        auth(receiver, channel["receiver"])
        auth(old, channel["sender"])
        assert receiver.receive_json()["type"] == "peer_status"

        with connect(client, channel["slug"]) as new:
            assert auth(new, channel["sender"])["role"] == "sender"
            expect_close(old, 4409)

            # Собеседник не видит offline/online: следующее его событие — уже сообщение
            new.send_json({"type": "send", "text": "from new tab"})
            assert receiver.receive_json()["text"] == "from new tab"


# --- сообщения ---


def test_send_reaches_both(client, channel):
    with connect(client, channel["slug"]) as receiver, connect(client, channel["slug"]) as sender:
        auth(receiver, channel["receiver"])
        auth(sender, channel["sender"])
        receiver.receive_json()  # peer_status

        sender.send_json({"type": "send", "text": "привет"})

        for ws in (receiver, sender):
            message = ws.receive_json()
            assert message["type"] == "message"
            assert message["text"] == "привет"
            assert {"id", "created_at", "expires_at"} <= message.keys()


def test_state_contains_live_messages(client, channel):
    with connect(client, channel["slug"]) as sender:
        auth(sender, channel["sender"])
        sender.send_json({"type": "send", "text": "первое"})
        sender.receive_json()

    with connect(client, channel["slug"]) as receiver:
        state = auth(receiver, channel["receiver"])

    assert [m["text"] for m in state["messages"]] == ["первое"]


def test_send_by_receiver_is_error(client, channel, engine):
    with connect(client, channel["slug"]) as receiver:
        auth(receiver, channel["receiver"])
        receiver.send_json({"type": "send", "text": "nope"})
        error = receiver.receive_json()

    assert error["code"] == "not_sender"
    assert count_messages(engine) == 0


@pytest.mark.parametrize(
    "event",
    [
        {"type": "send", "text": ""},
        {"type": "send", "text": "x" * (NOTE_TEXT_MAX_LENGTH + 1)},
        {"type": "unknown"},
        {"text": "no type"},
    ],
    ids=["empty_text", "too_long", "unknown_type", "no_type"],
)
def test_invalid_event_is_error_and_connection_stays(client, channel, engine, event):
    with connect(client, channel["slug"]) as sender:
        auth(sender, channel["sender"])
        sender.send_json(event)
        error = sender.receive_json()

        sender.send_json({"type": "send", "text": "still works"})
        assert sender.receive_json()["text"] == "still works"

    assert error["type"] == "error"
    assert error["code"] == "invalid_event"
    assert count_messages(engine) == 1


# --- смена ролей ---


def test_become_sender_swaps_roles_for_both(client, channel):
    with connect(client, channel["slug"]) as receiver, connect(client, channel["slug"]) as sender:
        auth(receiver, channel["receiver"])
        auth(sender, channel["sender"])
        receiver.receive_json()  # peer_status

        receiver.send_json({"type": "become_sender"})
        assert receiver.receive_json() == {"type": "roles_changed", "role": "sender"}
        assert sender.receive_json() == {"type": "roles_changed", "role": "receiver"}

        sender.send_json({"type": "send", "text": "old sender"})
        assert sender.receive_json()["code"] == "not_sender"

        receiver.send_json({"type": "send", "text": "new sender"})
        assert receiver.receive_json()["text"] == "new sender"
        assert sender.receive_json()["text"] == "new sender"


def test_become_sender_alone_then_peer_joins_as_receiver(client):
    created = client.post(URL).json()

    with connect(client, created["slug"]) as creator:
        auth(creator, created["token"])
        creator.send_json({"type": "become_sender"})
        assert creator.receive_json() == {"type": "roles_changed", "role": "sender"}

        joined = client.post(f"{URL}/{created['slug']}/join").json()
        assert joined["role"] == "receiver"

        with connect(client, created["slug"]) as peer:
            assert auth(peer, joined["token"])["role"] == "receiver"
            assert creator.receive_json()["type"] == "peer_status"

            creator.send_json({"type": "send", "text": "от создателя"})
            assert peer.receive_json()["text"] == "от создателя"


def test_become_sender_by_sender_is_error(client, channel):
    with connect(client, channel["slug"]) as sender:
        auth(sender, channel["sender"])
        sender.send_json({"type": "become_sender"})
        error = sender.receive_json()

    assert error["code"] == "not_receiver"


def test_events_during_state_load_come_after_state(client, channel, monkeypatch):
    # Отправитель грузит state, и в этот момент получатель забирает роль. Без буфера
    # roles_changed пришёл бы раньше state, и устаревший state вернул бы роль sender
    state_loaded = threading.Event()
    original_load_state = channels_ws._load_state

    def slow_load_state(db, participant_id):
        result = original_load_state(db, participant_id)
        state_loaded.set()
        time.sleep(0.3)
        return result

    with connect(client, channel["slug"]) as receiver:
        auth(receiver, channel["receiver"])
        monkeypatch.setattr(channels_ws, "_load_state", slow_load_state)

        with connect(client, channel["slug"]) as sender:
            sender.send_json({"type": "auth", "token": channel["sender"]})
            assert state_loaded.wait(timeout=5)
            receiver.send_json({"type": "become_sender"})
            assert receiver.receive_json()["type"] == "peer_status"
            assert receiver.receive_json() == {"type": "roles_changed", "role": "sender"}

            state = sender.receive_json()
            assert state["type"] == "state"
            assert state["role"] == "sender"
            assert sender.receive_json() == {"type": "roles_changed", "role": "receiver"}


# --- бездействие ---


def test_idle_channel_is_closed(client, channel, monkeypatch):
    monkeypatch.setattr(settings, "CHANNEL_IDLE_TIMEOUT_SECONDS", 1)

    with connect(client, channel["slug"]) as receiver, connect(client, channel["slug"]) as sender:
        auth(receiver, channel["receiver"])
        auth(sender, channel["sender"])
        assert receiver.receive_json()["type"] == "peer_status"

        for ws in (receiver, sender):
            assert ws.receive_json() == {"type": "channel_closed", "reason": "idle"}
            expect_close(ws, 4410)

    with connect(client, channel["slug"]) as ws:
        ws.send_json({"type": "auth", "token": channel["receiver"]})
        expect_close(ws, 4404)


def test_activity_postpones_idle_close(client, channel, monkeypatch):
    monkeypatch.setattr(settings, "CHANNEL_IDLE_TIMEOUT_SECONDS", 1)

    with connect(client, channel["slug"]) as sender:
        auth(sender, channel["sender"])
        # Каждое сообщение сдвигает last_activity_at: за 1,6 с канал не закрылся
        for i in range(5):
            if i:
                time.sleep(0.4)
            sender.send_json({"type": "send", "text": str(i)})
            assert sender.receive_json()["text"] == str(i)

        assert sender.receive_json() == {"type": "channel_closed", "reason": "idle"}
