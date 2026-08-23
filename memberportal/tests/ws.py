"""Helpers for driving the device WebSocket protocol in tests."""

import hashlib

from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from membermatters.websocket_urls import urlpatterns

# Route through the project's real URLRouter so path parsing (which populates
# scope["url_route"]["kwargs"]["device_id"]) is exercised too. The production
# stack wraps this in AuthMiddlewareStack, but devices never use Django auth.
TIMEOUT = 5


def connect_url(device_type: str, serial: str) -> str:
    return f"/ws/access/{device_type}/{serial}"


def make_communicator(device_type: str, serial: str) -> WebsocketCommunicator:
    return WebsocketCommunicator(
        URLRouter(urlpatterns), connect_url(device_type, serial)
    )


async def open_device(device_type: str, serial: str) -> WebsocketCommunicator:
    comm = make_communicator(device_type, serial)
    connected, _ = await comm.connect(timeout=TIMEOUT)
    assert connected, f"{device_type} {serial} failed to connect"
    return comm


async def authenticate(comm, raw_key: str) -> dict:
    """Perform the auth handshake and drain everything the server pushes.

    On success the server replies ``{"authorised": True}`` and then immediately
    pushes an initial state burst — a tag sync (doors only) and the current
    lockout state. Returns the messages keyed for convenient assertion.
    """
    await comm.send_json_to({"command": "authenticate", "secret_key": raw_key})
    ack = await comm.receive_json_from(timeout=TIMEOUT)

    burst = []
    if ack.get("authorised"):
        while True:
            if await comm.receive_nothing(timeout=0.35):
                break
            burst.append(await comm.receive_json_from(timeout=TIMEOUT))

    return {
        "ack": ack,
        "burst": burst,
        "by_command": {m.get("command"): m for m in burst},
    }


async def open_authenticated(device_type: str, serial: str, raw_key: str):
    """Connect + authenticate in one step. Returns (communicator, handshake)."""
    comm = await open_device(device_type, serial)
    handshake = await authenticate(comm, raw_key)
    assert handshake["ack"] == {"authorised": True}
    return comm, handshake


def expected_tag_hash(tags) -> str:
    """Reproduce the firmware cache key exactly as ``AccessControlledDevice.get_tags`` builds it.

    It is ``md5(str(<python list>))`` — the repr of a list, not JSON. The
    firmware's offline cache depends on this byte-for-byte, so it is a real
    contract and not an implementation detail.
    """
    return hashlib.md5(str(list(tags)).encode("utf-8")).hexdigest()


aget = database_sync_to_async
