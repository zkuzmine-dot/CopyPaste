import anyio
import pytest
from httpx2.websockets import WebSocketDisconnect
from sqlalchemy import func, select, update

from app.api.channels import ws as channels_ws
from app.db.models.channel import Channel, Message, Participant
from app.db.models.file import FileOrigin
from app.schemas.channel import MESSAGE_PAYLOAD_MAX_BYTES, MESSAGE_PAYLOAD_MIN_BYTES
from app.services import channels as channels_service
from app.services import files as files_service
from app.settings import settings
from app.storage.base import new_storage_key
from tests.channel_keys import (
    b64url,
    device_keys,
    exchange_keys_by_token,
    fake_payload_b64,
    join_by_http,
    text_of,
)

pytestmark = pytest.mark.anyio

URL = "/api/channels"
# Больше 65 КиБ по умолчанию у клиента: самый длинный шифротекст в base64 — около 400 КБ
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
async def channel(client, factory) -> dict:
    """Оба устройства в канале, ключи обменяны — можно писать."""
    keys = device_keys()
    created = (await client.post(URL)).json()
    joined = (await join_by_http(client, created["slug"], keys)).json()
    async with factory() as db, db.begin():
        await exchange_keys_by_token(db, created["slug"], joined["token"], keys)
    return {
        "slug": created["slug"],
        "receiver": created["token"],
        "sender": joined["token"],
        "sender_keys": keys,
    }


@pytest.fixture
async def pending(client) -> dict:
    """Второе устройство вошло, но ключами ещё не обменялись."""
    keys = device_keys()
    created = (await client.post(URL)).json()
    joined = (await join_by_http(client, created["slug"], keys)).json()
    return {
        "slug": created["slug"],
        "creator": created["token"],
        "joiner": joined["token"],
        "joiner_keys": keys,
    }


def connect(client, slug: str):
    return client.websocket(f"/ws/channels/{slug}", max_message_size_bytes=CLIENT_MAX_MESSAGE_BYTES)


def send_text(label: str) -> dict:
    return {"type": "send", "payload": fake_payload_b64(label)}


async def auth(ws, token: str) -> dict:
    await ws.send_json({"type": "auth", "token": token})
    return await ws.receive_json()


async def receive(ws, seconds: float = 5) -> dict:
    # Ответ, которого при поломке может не быть: тест падает сразу, а не когда канал
    # закроется по бездействию
    with anyio.fail_after(seconds):
        return await ws.receive_json()


async def expect_close(ws, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        await ws.receive_json()
    assert exc.value.code == code


async def count_messages(factory) -> int:
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(Message))


async def stored_keys(factory) -> list[bytes | None]:
    async with factory() as db:
        return list(await db.scalars(select(Participant.public_key).order_by(Participant.id)))


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
        "key_sent": True,
        "peer_key_commitment": channel["sender_keys"].commitment_b64,
        "peer_public_key": channel["sender_keys"].public_b64,
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
        send_text("hi"),
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
            await new.send_json(send_text("from new tab"))
            assert text_of(await receiver.receive_json()) == "from new tab"


# --- сообщения ---


async def test_send_reaches_both(client, channel):
    async with (
        connect(client, channel["slug"]) as receiver,
        connect(client, channel["slug"]) as sender,
    ):
        await auth(receiver, channel["receiver"])
        await auth(sender, channel["sender"])
        await receiver.receive_json()  # peer_status

        await sender.send_json(send_text("привет"))

        for ws in (receiver, sender):
            message = await ws.receive_json()
            assert message["type"] == "message"
            assert text_of(message) == "привет"
            assert {"id", "created_at", "expires_at"} <= message.keys()


async def test_state_contains_live_messages(client, channel):
    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json(send_text("первое"))
        await sender.receive_json()

    async with connect(client, channel["slug"]) as receiver:
        state = await auth(receiver, channel["receiver"])

    assert [text_of(m) for m in state["messages"]] == ["первое"]


async def test_send_by_receiver_is_error(client, channel, factory):
    async with connect(client, channel["slug"]) as receiver:
        await auth(receiver, channel["receiver"])
        await receiver.send_json(send_text("nope"))
        error = await receiver.receive_json()

    assert error["code"] == "not_sender"
    assert await count_messages(factory) == 0


@pytest.mark.parametrize(
    "event",
    [
        {"type": "send", "payload": b64url(bytes(MESSAGE_PAYLOAD_MIN_BYTES - 1))},
        {"type": "send", "payload": b64url(bytes(MESSAGE_PAYLOAD_MAX_BYTES + 1))},
        {"type": "send", "payload": "не base64"},
        # 4n+1 знаков base64 не бывает: такой строке не подобрать «=»
        {"type": "send", "payload": "A" * 45},
        # Открытый текст, как слали страницы до шифрования
        {"type": "send", "text": "открытый текст"},
        {"type": "unknown"},
        {"text": "no type"},
    ],
    ids=[
        "too_short",
        "too_long",
        "not_base64url",
        "impossible_length",
        "plain_text",
        "unknown_type",
        "no_type",
    ],
)
async def test_invalid_event_is_error_and_connection_stays(client, channel, factory, event):
    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json(event)
        error = await sender.receive_json()

        await sender.send_json(send_text("still works"))
        assert text_of(await sender.receive_json()) == "still works"

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

        await sender.send_json(send_text("old sender"))
        assert (await sender.receive_json())["code"] == "not_sender"

        await receiver.send_json(send_text("new sender"))
        assert text_of(await receiver.receive_json()) == "new sender"
        assert text_of(await sender.receive_json()) == "new sender"


async def test_become_sender_alone_then_peer_joins_as_receiver(client):
    created = (await client.post(URL)).json()
    creator_keys, peer_keys = device_keys(), device_keys()

    async with connect(client, created["slug"]) as creator:
        await auth(creator, created["token"])
        await creator.send_json({"type": "become_sender"})
        assert await creator.receive_json() == {"type": "roles_changed", "role": "sender"}

        joined = (await join_by_http(client, created["slug"], peer_keys)).json()
        assert joined["role"] == "receiver"
        assert (await creator.receive_json())["type"] == "peer_joined"

        async with connect(client, created["slug"]) as peer:
            assert (await auth(peer, joined["token"]))["role"] == "receiver"
            assert (await creator.receive_json())["type"] == "peer_status"

            await creator.send_json({"type": "public_key", "key": creator_keys.public_b64})
            assert (await peer.receive_json())["type"] == "peer_key"
            await peer.send_json({"type": "public_key", "key": peer_keys.public_b64})
            assert (await creator.receive_json())["type"] == "peer_key"

            await creator.send_json(send_text("от создателя"))
            assert text_of(await peer.receive_json()) == "от создателя"


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


# --- обмен ключами ---


async def test_join_sends_commitment_to_creator(client):
    created = (await client.post(URL)).json()
    keys = device_keys()

    async with connect(client, created["slug"]) as creator:
        assert (await auth(creator, created["token"]))["peer_key_commitment"] is None
        await join_by_http(client, created["slug"], keys)

        event = await receive(creator)

    assert event == {"type": "peer_joined", "key_commitment": keys.commitment_b64}


async def test_key_exchange_over_websocket(client, pending, factory):
    creator_keys, joiner_keys = device_keys(), pending["joiner_keys"]

    async with (
        connect(client, pending["slug"]) as creator,
        connect(client, pending["slug"]) as joiner,
    ):
        creator_state = await auth(creator, pending["creator"])
        joiner_state = await auth(joiner, pending["joiner"])
        assert creator_state["peer_key_commitment"] == joiner_keys.commitment_b64
        assert joiner_state["peer_public_key"] is None
        assert (await receive(creator))["type"] == "peer_status"

        await creator.send_json({"type": "public_key", "key": creator_keys.public_b64})
        assert await receive(joiner) == {"type": "peer_key", "key": creator_keys.public_b64}

        await joiner.send_json({"type": "public_key", "key": joiner_keys.public_b64})
        assert await receive(creator) == {"type": "peer_key", "key": joiner_keys.public_b64}

        await joiner.send_json(send_text("теперь можно"))
        assert text_of(await receive(creator)) == "теперь можно"

    assert await stored_keys(factory) == [creator_keys.public, joiner_keys.public]


async def test_state_resumes_key_exchange_after_reconnect(client, pending):
    creator_keys = device_keys()
    async with connect(client, pending["slug"]) as creator:
        await auth(creator, pending["creator"])
        await creator.send_json({"type": "public_key", "key": creator_keys.public_b64})
        await creator.send_json({"type": "become_sender"})
        # Ответ на become_sender приходит после обработки ключа: события идут по порядку
        assert (await receive(creator))["type"] == "roles_changed"

    async with connect(client, pending["slug"]) as creator:
        creator_state = await auth(creator, pending["creator"])
    async with connect(client, pending["slug"]) as joiner:
        joiner_state = await auth(joiner, pending["joiner"])

    assert creator_state["key_sent"] is True
    assert creator_state["peer_key_commitment"] == pending["joiner_keys"].commitment_b64
    assert creator_state["peer_public_key"] is None
    assert joiner_state["key_sent"] is False
    assert joiner_state["peer_public_key"] == creator_keys.public_b64


async def test_creator_key_before_join_is_error(client, factory):
    created = (await client.post(URL)).json()

    async with connect(client, created["slug"]) as creator:
        await auth(creator, created["token"])
        await creator.send_json({"type": "public_key", "key": device_keys().public_b64})
        error = await receive(creator)

    assert error["code"] == "key_out_of_order"
    assert await stored_keys(factory) == [None]


async def test_joiner_key_before_creator_key_is_error(client, pending, factory):
    async with connect(client, pending["slug"]) as creator:
        await auth(creator, pending["creator"])
        async with connect(client, pending["slug"]) as joiner:
            await auth(joiner, pending["joiner"])
            assert (await receive(creator))["type"] == "peer_status"

            await joiner.send_json({"type": "public_key", "key": pending["joiner_keys"].public_b64})
            assert (await receive(joiner))["code"] == "key_out_of_order"

        # Ключ не переслан: следующее событие у создателя — уже уход второго
        assert await receive(creator) == {"type": "peer_status", "online": False}

    assert await stored_keys(factory) == [None, None]


async def test_joiner_key_not_matching_commitment_is_error(client, pending, factory):
    async with (
        connect(client, pending["slug"]) as creator,
        connect(client, pending["slug"]) as joiner,
    ):
        await auth(creator, pending["creator"])
        await auth(joiner, pending["joiner"])
        assert (await receive(creator))["type"] == "peer_status"
        await creator.send_json({"type": "public_key", "key": device_keys().public_b64})
        assert (await receive(joiner))["type"] == "peer_key"

        await joiner.send_json({"type": "public_key", "key": device_keys().public_b64})
        assert (await receive(joiner))["code"] == "key_mismatch"

        await joiner.send_json(send_text("без ключей"))
        assert (await receive(joiner))["code"] == "keys_not_ready"

    creator_key, joiner_key = await stored_keys(factory)
    assert creator_key is not None and joiner_key is None


async def test_key_sent_twice_is_error(client, pending, factory):
    first = device_keys()

    async with connect(client, pending["slug"]) as creator:
        await auth(creator, pending["creator"])
        await creator.send_json({"type": "public_key", "key": first.public_b64})
        await creator.send_json({"type": "public_key", "key": device_keys().public_b64})
        error = await receive(creator)

    assert error["code"] == "key_already_sent"
    assert (await stored_keys(factory))[0] == first.public


@pytest.mark.parametrize(
    "key",
    [
        "не base64",
        b64url(bytes(65)),
        b64url(b"\x04" + bytes(64)),
        b64url(device_keys().public[:-1]),
        # Сжатая точка на той же кривой: отпечаток считается от байтов, поэтому формат один
        b64url(b"\x02" + device_keys().public[1:33]),
        123,
    ],
    ids=["not_base64url", "zeros", "not_on_curve", "short", "compressed", "not_string"],
)
async def test_invalid_public_key_is_invalid_event(client, pending, factory, key):
    async with connect(client, pending["slug"]) as creator:
        await auth(creator, pending["creator"])
        await creator.send_json({"type": "public_key", "key": key})
        error = await receive(creator)

        await creator.send_json({"type": "public_key", "key": device_keys().public_b64})
        await creator.send_json({"type": "become_sender"})
        assert (await receive(creator))["type"] == "roles_changed"

    assert error["code"] == "invalid_event"
    assert (await stored_keys(factory))[0] is not None


async def test_send_before_keys_is_error(client, pending, factory):
    async with connect(client, pending["slug"]) as joiner:
        await auth(joiner, pending["joiner"])
        await joiner.send_json(send_text("рано"))
        error = await receive(joiner)

    assert error["code"] == "keys_not_ready"
    assert await count_messages(factory) == 0


async def test_send_file_before_keys_is_error(client, pending, factory):
    async with factory() as db, db.begin():
        channel = await channels_service.get_active_channel(db, pending["slug"])
        joiner = await channels_service.get_participant(db, channel, pending["joiner"])
        stored = await files_service.create_upload(
            db,
            storage_key=new_storage_key(),
            name="a.txt",
            content_type="application/octet-stream",
            size_bytes=1,
            origin=FileOrigin.CHANNEL,
            uploaded_by_id=joiner.id,
        )

    async with connect(client, pending["slug"]) as ws:
        await auth(ws, pending["joiner"])
        await ws.send_json(
            {
                "type": "send_file",
                "file_id": stored.public_id,
                "payload": fake_payload_b64("meta"),
            }
        )
        error = await receive(ws)

    assert error["code"] == "keys_not_ready"
    assert await count_messages(factory) == 0


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
    monkeypatch.setattr(settings, "CHANNEL_IDLE_TIMEOUT_SECONDS", 3)

    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        # Каждое сообщение сдвигает last_activity_at: за 3,2 с при таймауте 3 с канал не
        # закрылся. Запас на промежуток — 2,2 с: таймер сверяется с часами БД, а их задержка
        # или прыжок (в Docker Desktop — до 1,6 с) при запасе в доли секунды закрывали канал
        for i in range(5):
            if i:
                await anyio.sleep(0.8)
            await sender.send_json(send_text(str(i)))
            assert text_of(await sender.receive_json()) == str(i)

        assert await sender.receive_json() == {"type": "channel_closed", "reason": "idle"}


async def test_channel_deleted_by_cleanup_closes_on_next_action(client, channel, factory):
    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        async with factory() as db, db.begin():
            active = await channels_service.get_active_channel(db, channel["slug"])
            await channels_service.close_channel(db, active)
            assert await channels_service.delete_inactive_channels(db) == 1

        await sender.send_json(send_text("в пустоту"))
        assert await sender.receive_json() == {"type": "channel_closed", "reason": "idle"}
        await expect_close(sender, 4410)


# --- лимиты ---


async def test_rate_limit(client, channel, factory, monkeypatch):
    monkeypatch.setattr(settings, "WS_RATE_LIMIT_BURST", 2)
    monkeypatch.setattr(settings, "WS_RATE_LIMIT_PER_SECOND", 0.001)

    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        for i in range(3):
            await sender.send_json(send_text(str(i)))
        events = [await sender.receive_json() for _ in range(3)]

    assert [text_of(e) for e in events[:2]] == ["0", "1"]
    assert events[2]["code"] == "rate_limited"
    assert await count_messages(factory) == 2


async def test_too_big_message_closes_1009(client, channel, factory, monkeypatch):
    monkeypatch.setattr(settings, "WS_MAX_MESSAGE_BYTES", 100)

    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json(send_text("x" * 100))
        await expect_close(sender, 1009)

    assert await count_messages(factory) == 0


async def test_too_big_auth_closes_1009(client, channel, monkeypatch):
    monkeypatch.setattr(settings, "WS_MAX_MESSAGE_BYTES", 100)

    async with connect(client, channel["slug"]) as ws:
        await ws.send_json({"type": "auth", "token": channel["sender"] + "x" * 100})
        await expect_close(ws, 1009)


async def test_size_limit_fits_longest_text(client, channel):
    # Самый длинный текст — 100 000 знаков по 3 байта в UTF-8, его шифротекст в base64
    payload = b64url(bytes(MESSAGE_PAYLOAD_MAX_BYTES))

    async with connect(client, channel["slug"]) as sender:
        await auth(sender, channel["sender"])
        await sender.send_json({"type": "send", "payload": payload})
        assert (await sender.receive_json())["payload"] == payload
