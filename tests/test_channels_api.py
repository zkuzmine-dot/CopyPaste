import pytest

from app.services import channels as channels_service
from app.utils.qr import make_qr_svg
from app.utils.slug import SLUG_ALPHABET, SLUG_LENGTH

URL = "/api/channels"


@pytest.fixture
def created(client) -> dict:
    response = client.post(URL)
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def joined(client, created) -> dict:
    response = client.post(f"{URL}/{created['slug']}/join")
    assert response.status_code == 201
    return response.json()


def get_state(client, slug: str, token: str | None):
    headers = {"X-Participant-Token": token} if token else {}
    return client.get(f"{URL}/{slug}", headers=headers)


# --- POST /api/channels ---


def test_create_channel(client):
    response = client.post(URL)

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "receiver"
    assert body["token"]
    assert len(body["slug"]) == SLUG_LENGTH
    assert set(body["slug"]) <= set(SLUG_ALPHABET)


# --- POST /api/channels/{slug}/join ---


def test_join_channel(created, joined):
    assert joined["role"] == "sender"
    assert joined["slug"] == created["slug"]
    assert joined["token"] != created["token"]


def test_join_full_channel(client, created, joined):
    response = client.post(f"{URL}/{created['slug']}/join")

    assert response.status_code == 409
    assert response.json() == {"detail": "Channel is full"}


def test_join_unknown_channel(client):
    response = client.post(f"{URL}/zzzzzzzz/join")

    assert response.status_code == 404


# --- GET /api/channels/{slug} ---


def test_state_before_join(client, created):
    response = get_state(client, created["slug"], created["token"])

    assert response.status_code == 200
    assert response.json() == {
        "slug": created["slug"],
        "role": "receiver",
        "peer_joined": False,
        "messages": [],
    }


def test_state_after_join(client, created, joined):
    receiver_state = get_state(client, created["slug"], created["token"]).json()
    sender_state = get_state(client, created["slug"], joined["token"]).json()

    assert (receiver_state["role"], receiver_state["peer_joined"]) == ("receiver", True)
    assert (sender_state["role"], sender_state["peer_joined"]) == ("sender", True)


def test_state_shows_messages(client, db, created, joined):
    channel = channels_service.get_active_channel(db, created["slug"])
    sender = channels_service.get_participant(db, channel, joined["token"])
    channels_service.add_message(db, sender, "hello")

    messages = get_state(client, created["slug"], created["token"]).json()["messages"]

    assert [m["text"] for m in messages] == ["hello"]
    assert set(messages[0]) == {"id", "kind", "text", "file", "created_at", "expires_at"}
    assert messages[0]["kind"] == "text"
    assert messages[0]["file"] is None


@pytest.mark.parametrize("token", [None, "wrong-token"], ids=["no-token", "wrong-token"])
def test_state_invalid_token(client, created, token):
    response = get_state(client, created["slug"], token)

    assert response.status_code == 401


def test_state_token_from_other_channel(client, created):
    other = client.post(URL).json()

    response = get_state(client, created["slug"], other["token"])

    assert response.status_code == 401


def test_state_unknown_channel(client, created):
    response = get_state(client, "zzzzzzzz", created["token"])

    assert response.status_code == 404


def test_state_closed_channel(client, db, created):
    channel = channels_service.get_active_channel(db, created["slug"])
    channels_service.close_channel(db, channel)

    response = get_state(client, created["slug"], created["token"])

    assert response.status_code == 404


# --- QR ---


def test_channel_qr(client, created):
    response = client.get(f"{URL}/{created['slug']}/qr")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.text == make_qr_svg(f"http://testserver/c/{created['slug']}")


def test_channel_qr_unknown(client):
    response = client.get(f"{URL}/zzzzzzzz/qr")

    assert response.status_code == 404
