import pytest

from app.services import channels as channels_service
from app.utils.qr import make_qr_svg
from app.utils.slug import SLUG_ALPHABET, SLUG_LENGTH
from tests.channel_keys import (
    DeviceKeys,
    b64url,
    device_keys,
    exchange_keys_by_token,
    fake_payload,
    join_by_http,
    text_of,
)

pytestmark = pytest.mark.anyio

URL = "/api/channels"


@pytest.fixture
async def created(client) -> dict:
    response = await client.post(URL)
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def joiner_keys() -> DeviceKeys:
    return device_keys()


@pytest.fixture
async def joined(client, created, joiner_keys) -> dict:
    response = await join_by_http(client, created["slug"], joiner_keys)
    assert response.status_code == 201
    return response.json()


async def get_state(client, slug: str, token: str | None):
    headers = {"X-Participant-Token": token} if token else {}
    return await client.get(f"{URL}/{slug}", headers=headers)


# --- POST /api/channels ---


async def test_create_channel(client):
    response = await client.post(URL)

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "receiver"
    assert body["token"]
    assert len(body["slug"]) == SLUG_LENGTH
    assert set(body["slug"]) <= set(SLUG_ALPHABET)


# --- POST /api/channels/{slug}/join ---


async def test_join_channel(created, joined):
    assert joined["role"] == "sender"
    assert joined["slug"] == created["slug"]
    assert joined["token"] != created["token"]


async def test_join_full_channel(client, created, joined):
    response = await join_by_http(client, created["slug"])

    assert response.status_code == 409
    assert response.json() == {"detail": "Channel is full"}


async def test_join_unknown_channel(client):
    response = await join_by_http(client, "zzzzzzzz")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"key_commitment": b64url(bytes(31))},
        {"key_commitment": b64url(bytes(33))},
        {"key_commitment": b64url(bytes(32)) + "="},
        {"key_commitment": "+" * 43},
        {"key_commitment": 123},
    ],
    ids=["no_body", "no_field", "short", "long", "padding", "not_base64url", "not_string"],
)
async def test_join_requires_valid_commitment(client, db, created, body):
    response = await client.post(f"{URL}/{created['slug']}/join", json=body)

    assert response.status_code == 422
    channel = await channels_service.get_active_channel(db, created["slug"])
    assert await channels_service.count_participants(db, channel) == 1


# --- GET /api/channels/{slug} ---


async def test_state_before_join(client, created):
    response = await get_state(client, created["slug"], created["token"])

    assert response.status_code == 200
    assert response.json() == {
        "slug": created["slug"],
        "role": "receiver",
        "peer_joined": False,
        "messages": [],
        "key_sent": False,
        "peer_key_commitment": None,
        "peer_public_key": None,
    }


async def test_state_after_join(client, created, joined, joiner_keys):
    receiver_state = (await get_state(client, created["slug"], created["token"])).json()
    sender_state = (await get_state(client, created["slug"], joined["token"])).json()

    assert (receiver_state["role"], receiver_state["peer_joined"]) == ("receiver", True)
    assert (sender_state["role"], sender_state["peer_joined"]) == ("sender", True)
    # Отпечаток второго видит создатель — он отвечает на него своим ключом
    assert receiver_state["peer_key_commitment"] == joiner_keys.commitment_b64
    assert sender_state["peer_key_commitment"] is None


async def test_state_after_key_exchange(client, db, created, joined, joiner_keys):
    await exchange_keys_by_token(db, created["slug"], joined["token"], joiner_keys)

    receiver_state = (await get_state(client, created["slug"], created["token"])).json()
    sender_state = (await get_state(client, created["slug"], joined["token"])).json()

    assert receiver_state["key_sent"] and sender_state["key_sent"]
    assert receiver_state["peer_public_key"] == joiner_keys.public_b64
    assert sender_state["peer_public_key"] is not None


async def test_state_shows_messages(client, db, created, joined, joiner_keys):
    await exchange_keys_by_token(db, created["slug"], joined["token"], joiner_keys)
    channel = await channels_service.get_active_channel(db, created["slug"])
    sender = await channels_service.get_participant(db, channel, joined["token"])
    await channels_service.add_message(db, sender, fake_payload("hello"))

    messages = (await get_state(client, created["slug"], created["token"])).json()["messages"]

    assert [text_of(m) for m in messages] == ["hello"]
    assert set(messages[0]) == {"id", "kind", "payload", "file", "created_at", "expires_at"}
    assert messages[0]["kind"] == "text"
    assert messages[0]["file"] is None


@pytest.mark.parametrize("token", [None, "wrong-token"], ids=["no-token", "wrong-token"])
async def test_state_invalid_token(client, created, token):
    response = await get_state(client, created["slug"], token)

    assert response.status_code == 401


async def test_state_token_from_other_channel(client, created):
    other = (await client.post(URL)).json()

    response = await get_state(client, created["slug"], other["token"])

    assert response.status_code == 401


async def test_state_unknown_channel(client, created):
    response = await get_state(client, "zzzzzzzz", created["token"])

    assert response.status_code == 404


async def test_state_closed_channel(client, db, created):
    channel = await channels_service.get_active_channel(db, created["slug"])
    await channels_service.close_channel(db, channel)

    response = await get_state(client, created["slug"], created["token"])

    assert response.status_code == 404


# --- QR ---


async def test_channel_qr(client, created):
    response = await client.get(f"{URL}/{created['slug']}/qr")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.text == make_qr_svg(f"http://test/c/{created['slug']}")


async def test_channel_qr_unknown(client):
    response = await client.get(f"{URL}/zzzzzzzz/qr")

    assert response.status_code == 404
