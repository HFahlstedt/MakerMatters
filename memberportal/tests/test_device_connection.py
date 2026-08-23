"""Characterisation: device connection and authentication handshake.

This is the contract every ESP32 door, interlock and vending device depends on.
It is also the part most exposed by the channels 3 -> 4 upgrade, which changes
how consumers are instantiated and run.

Every test here runs with ``transaction=True`` because sync consumers execute
in a worker thread with their own database connection; the default
transaction-wrapped ``django_db`` would hide test data from them.
"""

import pytest

from tests import ws

pytestmark = [pytest.mark.protocol, pytest.mark.django_db(transaction=True)]


DEVICE_TYPES = ["door", "interlock", "memberbucks"]


@pytest.fixture
def make_device(make_door, make_interlock, make_memberbucks_device):
    """Create any of the three device types by protocol name."""
    factories = {
        "door": make_door,
        "interlock": make_interlock,
        "memberbucks": make_memberbucks_device,
    }

    def _make(device_type, serial, **kwargs):
        return factories[device_type](serial=serial, **kwargs)

    return _make


@pytest.mark.parametrize("device_type", DEVICE_TYPES)
async def test_authorised_device_can_connect(make_device, device_type):
    await ws.aget(make_device)(device_type, f"{device_type}-conn-01", authorised=True)

    comm = await ws.open_device(device_type, f"{device_type}-conn-01")
    await comm.disconnect()


@pytest.mark.parametrize("device_type", DEVICE_TYPES)
async def test_connecting_checks_the_device_in(make_device, device_type):
    """``last_seen`` drives the 3-minute offline heuristic used across the admin UI."""
    serial = f"{device_type}-conn-02"
    device = await ws.aget(make_device)(device_type, serial, authorised=True)
    assert device.last_seen is None

    comm = await ws.open_device(device_type, serial)
    await comm.disconnect()

    refreshed = await ws.aget(type(device).objects.get)(serial_number=serial)
    assert refreshed.last_seen is not None


async def test_unknown_serial_auto_commissions_a_device():
    """An unrecognised serial silently creates a device row.

    This is a real exposure, not a bug in the test: anyone who can reach the
    WebSocket endpoint can create ``Doors`` rows. The new device is created
    unauthorised and hidden, so it cannot open anything, but it does persist.
    Pinned here so the behaviour cannot change unnoticed during the upgrade.
    """
    from access.models import Doors

    serial = "never-seen-before-42"
    assert await ws.aget(Doors.objects.filter(serial_number=serial).count)() == 0

    comm = await ws.open_device("door", serial)
    await comm.disconnect()

    created = await ws.aget(Doors.objects.get)(serial_number=serial)
    assert created.authorised is False
    assert created.hidden is True
    assert created.report_online_status is False
    assert created.name == f"New Device ({serial})"


async def test_unauthorised_device_is_accepted_then_immediately_closed(make_door):
    """Note the handshake quirk: the socket is accepted *before* being closed.

    A client sees a successful connection followed by a close, rather than a
    rejected handshake. The firmware relies on this, so it is part of the contract.
    """
    await ws.aget(make_door)(serial="unauthorised-01", authorised=False)

    comm = ws.make_communicator("door", "unauthorised-01")
    connected, _ = await comm.connect(timeout=ws.TIMEOUT)
    assert connected is True

    output = await comm.receive_output(timeout=ws.TIMEOUT)
    assert output["type"] == "websocket.close"
    await comm.disconnect()


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@pytest.mark.parametrize("device_type", DEVICE_TYPES)
async def test_valid_key_authenticates(make_device, device_api_key, device_type):
    serial = f"{device_type}-auth-01"
    await ws.aget(make_device)(device_type, serial, authorised=True)

    comm = await ws.open_device(device_type, serial)
    handshake = await ws.authenticate(comm, device_api_key)

    assert handshake["ack"] == {"authorised": True}
    await comm.disconnect()


@pytest.mark.parametrize("device_type", DEVICE_TYPES)
async def test_invalid_key_is_rejected_and_disconnected(make_device, device_type):
    serial = f"{device_type}-auth-02"
    await ws.aget(make_device)(device_type, serial, authorised=True)

    comm = await ws.open_device(device_type, serial)
    await comm.send_json_to({"command": "authenticate", "secret_key": "not-a-real-key"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {"authorised": False}
    assert (await comm.receive_output(timeout=ws.TIMEOUT))["type"] == "websocket.close"
    await comm.disconnect()


async def test_any_valid_key_authenticates_any_device(make_door, device_api_key):
    """Device API keys are global, not bound to a device.

    A key issued for one device authenticates every other device. Pinned
    because it is load-bearing for how spaces provision hardware today — worth
    revisiting in the refactor, but changing it silently would break fleets.
    """
    await ws.aget(make_door)(serial="door-shared-key-a", authorised=True)
    await ws.aget(make_door)(serial="door-shared-key-b", authorised=True)

    for serial in ("door-shared-key-a", "door-shared-key-b"):
        comm, _ = await ws.open_authenticated("door", serial, device_api_key)
        await comm.disconnect()


async def test_commands_before_authentication_are_refused(make_door):
    await ws.aget(make_door)(serial="door-auth-gate", authorised=True)

    comm = await ws.open_device("door", "door-auth-gate")
    await comm.send_json_to({"command": "ping"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {"authorised": False}
    assert (await comm.receive_output(timeout=ws.TIMEOUT))["type"] == "websocket.close"
    await comm.disconnect()


# --------------------------------------------------------------------------
# Commands shared by every device type
# --------------------------------------------------------------------------


@pytest.mark.parametrize("device_type", DEVICE_TYPES)
async def test_ping_is_answered_with_pong(make_device, device_api_key, device_type):
    serial = f"{device_type}-ping"
    await ws.aget(make_device)(device_type, serial, authorised=True)

    comm, _ = await ws.open_authenticated(device_type, serial, device_api_key)
    await comm.send_json_to({"command": "ping"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {"command": "pong"}
    await comm.disconnect()


async def test_ip_address_command_is_persisted(make_door, device_api_key):
    from access.models import Doors

    await ws.aget(make_door)(serial="door-ip", authorised=True)

    comm, _ = await ws.open_authenticated("door", "door-ip", device_api_key)
    await comm.send_json_to({"command": "ip_address", "ip_address": "10.1.2.3"})
    await comm.receive_nothing(timeout=0.4)
    await comm.disconnect()

    device = await ws.aget(Doors.objects.get)(serial_number="door-ip")
    assert device.ip_address == "10.1.2.3"


async def test_unknown_command_is_ignored_without_dropping_the_socket(
    make_door, device_api_key
):
    """Forward compatibility: newer firmware may send commands this server predates."""
    await ws.aget(make_door)(serial="door-unknown-cmd", authorised=True)

    comm, _ = await ws.open_authenticated("door", "door-unknown-cmd", device_api_key)
    await comm.send_json_to({"command": "command_from_the_future"})
    assert await comm.receive_nothing(timeout=0.4)

    # The socket is still usable.
    await comm.send_json_to({"command": "ping"})
    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {"command": "pong"}
    await comm.disconnect()
