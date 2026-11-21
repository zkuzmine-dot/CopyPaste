import pytest

from app.services import channels as channels_service
from app.utils.qr import make_qr_svg
from app.utils.slug import SLUG_ALPHABET, SLUG_LENGTH

pytestmark = pytest.mark.anyio

URL = "/api/channels"


@pytest.fixture
async def created(client) -> dict:
    response = await client.post(URL)
    assert response.status_code == 201
    return response.json()


@pytest.fixture
async def joined(client, created) -> dict:
    response = await client.post(f"{URL}/{created['slug']}/join")
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
    response = await client.post(f"{URL}/{created['slug']}/join")

    assert response.status_code == 409
    assert response.json() == {"detail": "Channel is full"}


async def test_join_unknown_channel(client):
    response = await client.post(f"{URL}/zzzzzzzz/join")

    assert response.status_code == 404


# --- GET /api/channels/{slug} ---


async def test_state_before_join(client, created):
    response = await get_state(client, created["slug"], created["token"])

    assert response.status_code == 200
    assert response.json() == {
        "slug": created["slug"],
        "role": "receiver",
        "peer_joined": False,
        "messages": [],
    }


async def test_state_after_join(client, created, joined):
    receiver_state = (await get_state(client, created["slug"], created["token"])).json()
    sender_state = (await get_state(client, created["slug"], joined["token"])).json()

    assert (receiver_state["role"], receiver_state["peer_joined"]) == ("receiver", True)
    assert (sender_state["role"], sender_state["peer_joined"]) == ("sender", True)


async def test_state_shows_messages(client, async_db, created, joined):
    channel = await async_db.run_sync(channels_service.get_active_channel, created["slug"])
    sender = await async_db.run_sync(channels_service.get_participant, channel, joined["token"])
    await async_db.run_sync(channels_service.add_message, sender, "hello")

    messages = (await get_state(client, created["slug"], created["token"])).json()["messages"]

    assert [m["text"] for m in messages] == ["hello"]
    assert set(messages[0]) == {"id", "kind", "text", "file", "created_at", "expires_at"}
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


async def test_state_closed_channel(client, async_db, created):
    channel = await async_db.run_sync(channels_service.get_active_channel, created["slug"])
    await async_db.run_sync(channels_service.close_channel, channel)

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
