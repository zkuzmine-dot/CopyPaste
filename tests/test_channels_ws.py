import anyio
import pytest
from httpx2.websockets import WebSocketDisconnect
from sqlalchemy import func, select, update

from app.api.channels import ws as channels_ws
from app.db.models.channel import Channel, Message
from app.schemas.note import NOTE_TEXT_MAX_LENGTH
from app.services import channels as channels_service
from app.settings import settings

pytestmark = pytest.mark.anyio

URL = "/api/channels"
# Больше 65 КиБ по умолчанию у клиента: самый длинный текст в JSON — около 600 КБ
CLIENT_MAX_MESSAGE_BYTES = 4 * 1024 * 1024


@pytest.fixture
def client(committing_client):
    # Обработчик и таймер бездействия ходят в БД одновременно, а одно тестовое соединение
    # так не умеет — поэтому здесь настоящие коммиты
    return committing_client


@pytest.fixture
def factory(committing_session_factory):
    return committing_session_factory


@pytest.fixture
async def channel(client) -> dict:
    created = (await client.post(URL)).json()
    joined = (await client.post(f"{URL}/{created['slug']}/join")).json()
    return {"slug": created["slug"], "receiver": created["token"], "sender": joined["token"]}


def connect(client, slug: str):
    return client.websocket(f"/ws/channels/{slug}", max_message_size_bytes=CLIENT_MAX_MESSAGE_BYTES)


async def auth(ws, token: str) -> dict:
    await ws.send_json({"type": "auth", "token": token})
    return await ws.receive_json()


async def expect_close(ws, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        await ws.receive_json()
    assert exc.value.code == code


async def count_messages(factory) -> int:
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(Message))


# --- аутентификация ---


async def test_auth_returns_state(client, channel):
    async with connect(client, channel["slug"]) as ws:
        state = await auth(ws, channel["receiver"])

    assert state == {
        "type": "state",
        "role": "receiver",
        "peer_joined": True,
        "peer_online": False,
        "messages": [],
    }


async def test_state_before_peer_joined(client):
    created = (await client.post(URL)).json()

    async with connect(client, created["slug"]) as ws:
        state = await auth(ws, created["token"])

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
async def test_bad_auth_closes_4401(client, channel, first_message):
    async with connect(client, channel["slug"]) as ws:
        if isinstance(first_message, str):
            await ws.send_text(first_message)
        else:
            await ws.send_json(first_message)
        await expect_close(ws, 4401)


async def test_token_from_other_channel_closes_4401(client, channel):
    other = (await client.post(URL)).json()

    async with connect(client, channel["slug"]) as ws:
        await ws.send_json({"type": "auth", "token": other["token"]})
        await expect_close(ws, 4401)


async def test_auth_timeout_closes_4401(client, channel, monkeypatch):
    monkeypatch.setattr(settings, "WS_AUTH_TIMEOUT_SECONDS", 0.1)

    async with connect(client, channel["slug"]) as ws:
        await expect_close(ws, 4401)


async def test_unknown_channel_closes_4404(client, channel):
    async with connect(client, "zzzzzzzz") as ws:
        await ws.send_json({"type": "auth", "token": channel["receiver"]})
        await expect_close(ws, 4404)


async def test_closed_channel_closes_4404(client, channel, factory):
    async with factory() as db, db.begin():
        await db.execute(update(Channel).values(closed_at=func.now()))

    async with connect(client, channel["slug"]) as ws:
        await ws.send_json({"type": "auth", "token": channel["receiver"]})
        await expect_close(ws, 4404)


async def test_repeated_auth_is_error(client, channel):
    async with connect(client, channel["slug"]) as ws:
        await auth(ws, channel["receiver"])
        error = await auth(ws, channel["receiver"])

    assert error["type"] == "error"
    assert error["code"] == "already_authenticated"


# --- присутствие ---


async def test_peer_status(client, channel):
    async with connect(client, channel["slug"]) as receiver:
        await auth(receiver, channel["receiver"])

        async with connect(client, channel["slug"]) as sender:
            assert (await auth(sender, channel["sender"]))["peer_online"] is True
            assert await receiver.receive_json() == {"type": "peer_status", "online": True}

        assert await receiver.receive_json() == {"type": "peer_status", "online": False}


async def test_same_token_replaces_old_connection(client, channel):
    async with (
        connect(client, channel["slug"]) as receiver,
        connect(client, channel["slug"]) as old,
    ):
        await auth(receiver, channel["receiver"])
        await auth(old, channel["sender"])
        assert (await receiver.receive_json())["type"] == "peer_status"

        async with connect(client, channel["slug"]) as new:
            assert (await auth(new, channel["sender"]))["role"] == "sender"
            await expect_close(old, 4409)

            # Собеседник не видит offline/online: следующее его событие — уже сообщение
            await new.send_json({"type": "send", "text": "from new tab"})
            assert (await receiver.receive_json())["text"] == "from new tab"


# --- сообщения ---


async def test_send_reaches_both(client, channel):
    async with (
        connect(client, channel["slug"]) as receiver,
        connect(client, channel["slug"]) as sender,
    ):
        await auth(receiver, channel["receiver"])
        await auth(sender, channel["sender"])
        await receiver.receive_json()  # peer_status

        await sender.send_json({"type": "send", "text": "привет"})

        for ws in (receiver, sender):
            message = await ws.receive_json()
            assert message["type"] == "message"
            assert message["text"] == "привет"
            assert {"id", "created_at", "expires_at"} <= message.keys()


async def test_state_contains_live_messages(client, channel):
    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json({"type": "send", "text": "первое"})
        await sender.receive_json()

    async with connect(client, channel["slug"]) as receiver:
        state = await auth(receiver, channel["receiver"])

    assert [m["text"] for m in state["messages"]] == ["первое"]


async def test_send_by_receiver_is_error(client, channel, factory):
    async with connect(client, channel["slug"]) as receiver:
        await auth(receiver, channel["receiver"])
        await receiver.send_json({"type": "send", "text": "nope"})
        error = await receiver.receive_json()

    assert error["code"] == "not_sender"
    assert await count_messages(factory) == 0


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
async def test_invalid_event_is_error_and_connection_stays(client, channel, factory, event):
    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json(event)
        error = await sender.receive_json()

        await sender.send_json({"type": "send", "text": "still works"})
        assert (await sender.receive_json())["text"] == "still works"

    assert error["type"] == "error"
    assert error["code"] == "invalid_event"
    assert await count_messages(factory) == 1


# --- смена ролей ---


async def test_become_sender_swaps_roles_for_both(client, channel):
    async with (
        connect(client, channel["slug"]) as receiver,
        connect(client, channel["slug"]) as sender,
    ):
        await auth(receiver, channel["receiver"])
        await auth(sender, channel["sender"])
        await receiver.receive_json()  # peer_status

        await receiver.send_json({"type": "become_sender"})
        assert await receiver.receive_json() == {"type": "roles_changed", "role": "sender"}
        assert await sender.receive_json() == {"type": "roles_changed", "role": "receiver"}

        await sender.send_json({"type": "send", "text": "old sender"})
        assert (await sender.receive_json())["code"] == "not_sender"

        await receiver.send_json({"type": "send", "text": "new sender"})
        assert (await receiver.receive_json())["text"] == "new sender"
        assert (await sender.receive_json())["text"] == "new sender"


async def test_become_sender_alone_then_peer_joins_as_receiver(client):
    created = (await client.post(URL)).json()

    async with connect(client, created["slug"]) as creator:
        await auth(creator, created["token"])
        await creator.send_json({"type": "become_sender"})
        assert await creator.receive_json() == {"type": "roles_changed", "role": "sender"}

        joined = (await client.post(f"{URL}/{created['slug']}/join")).json()
        assert joined["role"] == "receiver"

        async with connect(client, created["slug"]) as peer:
            assert (await auth(peer, joined["token"]))["role"] == "receiver"
            assert (await creator.receive_json())["type"] == "peer_status"

            await creator.send_json({"type": "send", "text": "от создателя"})
            assert (await peer.receive_json())["text"] == "от создателя"


async def test_become_sender_by_sender_is_error(client, channel):
    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json({"type": "become_sender"})
        error = await sender.receive_json()

    assert error["code"] == "not_receiver"


async def test_events_during_state_load_come_after_state(client, channel, monkeypatch):
    # Отправитель загрузил state, но ещё не отправил, и в этот момент получатель забирает
    # роль. Без буфера roles_changed пришёл бы раньше state, и устаревший state вернул бы
    # роль sender
    state_loaded = anyio.Event()
    original_load_state = channels_ws._load_state
    original_run_db = channels_ws.run_db
    delayed = False

    async def load_state(db, participant_id):
        result = await original_load_state(db, participant_id)
        state_loaded.set()
        return result

    async def run_db_with_window(factory, fn):
        nonlocal delayed
        result = await original_run_db(factory, fn)
        if state_loaded.is_set() and not delayed:
            # Окно между загрузкой state и его отправкой — один раз, только для отправителя
            delayed = True
            await anyio.sleep(0.3)
        return result

    async with connect(client, channel["slug"]) as receiver:
        await auth(receiver, channel["receiver"])
        monkeypatch.setattr(channels_ws, "_load_state", load_state)
        monkeypatch.setattr(channels_ws, "run_db", run_db_with_window)

        async with connect(client, channel["slug"]) as sender:
            await sender.send_json({"type": "auth", "token": channel["sender"]})
            with anyio.fail_after(5):
                await state_loaded.wait()
            await receiver.send_json({"type": "become_sender"})
            assert (await receiver.receive_json())["type"] == "peer_status"
            assert await receiver.receive_json() == {"type": "roles_changed", "role": "sender"}

            state = await sender.receive_json()
            assert state["type"] == "state"
            assert state["role"] == "sender"
            assert await sender.receive_json() == {"type": "roles_changed", "role": "receiver"}


# --- бездействие ---


async def test_idle_channel_is_closed(client, channel, monkeypatch):
    monkeypatch.setattr(settings, "CHANNEL_IDLE_TIMEOUT_SECONDS", 1)

    async with (
        connect(client, channel["slug"]) as receiver,
        connect(client, channel["slug"]) as sender,
    ):
        await auth(receiver, channel["receiver"])
        await auth(sender, channel["sender"])
        assert (await receiver.receive_json())["type"] == "peer_status"

        for ws in (receiver, sender):
            assert await ws.receive_json() == {"type": "channel_closed", "reason": "idle"}
            await expect_close(ws, 4410)

    async with connect(client, channel["slug"]) as ws:
        await ws.send_json({"type": "auth", "token": channel["receiver"]})
        await expect_close(ws, 4404)


async def test_activity_postpones_idle_close(client, channel, monkeypatch):
    monkeypatch.setattr(settings, "CHANNEL_IDLE_TIMEOUT_SECONDS", 1)

    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        # Каждое сообщение сдвигает last_activity_at: за 1,6 с канал не закрылся
        for i in range(5):
            if i:
                await anyio.sleep(0.4)
            await sender.send_json({"type": "send", "text": str(i)})
            assert (await sender.receive_json())["text"] == str(i)

        assert await sender.receive_json() == {"type": "channel_closed", "reason": "idle"}


async def test_channel_deleted_by_cleanup_closes_on_next_action(client, channel, factory):
    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        async with factory() as db, db.begin():
            active = await channels_service.get_active_channel(db, channel["slug"])
            await channels_service.close_channel(db, active)
            assert await channels_service.delete_inactive_channels(db) == 1

        await sender.send_json({"type": "send", "text": "в пустоту"})
        assert await sender.receive_json() == {"type": "channel_closed", "reason": "idle"}
        await expect_close(sender, 4410)


# --- лимиты ---


async def test_rate_limit(client, channel, factory, monkeypatch):
    monkeypatch.setattr(settings, "WS_RATE_LIMIT_BURST", 2)
    monkeypatch.setattr(settings, "WS_RATE_LIMIT_PER_SECOND", 0.001)

    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        for i in range(3):
            await sender.send_json({"type": "send", "text": str(i)})
        events = [await sender.receive_json() for _ in range(3)]

    assert [e.get("text") for e in events[:2]] == ["0", "1"]
    assert events[2]["code"] == "rate_limited"
    assert await count_messages(factory) == 2


async def test_too_big_message_closes_1009(client, channel, factory, monkeypatch):
    monkeypatch.setattr(settings, "WS_MAX_MESSAGE_BYTES", 100)

    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json({"type": "send", "text": "x" * 100})
        await expect_close(sender, 1009)

    assert await count_messages(factory) == 0


async def test_too_big_auth_closes_1009(client, channel, monkeypatch):
    monkeypatch.setattr(settings, "WS_MAX_MESSAGE_BYTES", 100)

    async with connect(client, channel["slug"]) as ws:
        await ws.send_json({"type": "auth", "token": channel["sender"] + "x" * 100})
        await expect_close(ws, 1009)


async def test_size_limit_fits_longest_text(client, channel):
    # Худший случай для JSON: каждый символ экранируется как \u0001 — 6 байт
    text = "\u0001" * NOTE_TEXT_MAX_LENGTH

    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json({"type": "send", "text": text})
        assert (await sender.receive_json())["text"] == text
