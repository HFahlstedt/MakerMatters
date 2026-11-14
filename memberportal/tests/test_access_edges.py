"""Characterisation: the edges of the device protocol and the access models.

The per-type protocol suites cover the conversations a healthy device has. This
file is what they leave: commands the *server* pushes to a connected device,
what happens when a device names a session that does not exist or belongs to
another interlock, the vending machine's unknown-product branch, the status
endpoint's lockout count, the interlock model's rejection bookkeeping and the
Celery heartbeat.
"""

import types

import pytest

from tests import ws

@pytest.fixture
def make_device_of(make_door, make_interlock, make_memberbucks_device):
    makers = {
        "door": make_door,
        "interlock": make_interlock,
        "memberbucks": make_memberbucks_device,
    }

    def _make(device_type, serial):
        return makers[device_type](serial=serial, authorised=True)

    return _make


# --------------------------------------------------------------------------
# Commands pushed from the server to a connected device
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method, command", [("reboot", "reboot"), ("lock", "lock"), ("unlock", "unlock")])
@pytest.mark.parametrize("device_type", ["door", "interlock", "memberbucks"])
@pytest.mark.protocol
@pytest.mark.django_db(transaction=True)
async def test_an_admin_command_reaches_the_connected_device(
    make_device_of, device_api_key, device_type, method, command
):
    """``reboot``/``lock``/``unlock`` on the model arrive as the device's own packet."""
    serial = f"push-{device_type}-{method}"
    device = await ws.aget(make_device_of)(device_type, serial)
    comm, _ = await ws.open_authenticated(device_type, serial, device_api_key)

    assert await ws.aget(getattr(device, method))() is True

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {"command": command}
    await comm.disconnect()


@pytest.mark.protocol
@pytest.mark.django_db(transaction=True)
async def test_a_door_bump_reaches_the_connected_door(make_door, device_api_key):
    door = await ws.aget(make_door)(serial="push-bump")
    comm, _ = await ws.open_authenticated("door", "push-bump", device_api_key)

    await ws.aget(door.bump)()

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {"command": "bump"}
    await comm.disconnect()


@pytest.mark.protocol
@pytest.mark.django_db(transaction=True)
async def test_a_device_with_no_serial_number_is_not_sent_commands(make_door):
    door = await ws.aget(make_door)(serial="", name="No serial")

    assert await ws.aget(door.reboot)() is False


@pytest.mark.protocol
@pytest.mark.django_db(transaction=True)
async def test_a_sync_pushed_to_an_unauthenticated_device_is_ignored(
    make_door, device_api_key
):
    """Used to raise in the consumer and drop the socket.

    A connected device that has not yet sent a valid key is already in its
    channel group. An administrator's "sync" -- or the sync a sign-in or a
    membership change fans out to every affected door -- reached it, and
    ``check_authorised`` raised ``RuntimeError``, which unwound the consumer
    and dropped the connection. The event is now declined and logged, and the
    device can go on to authenticate.
    """
    door = await ws.aget(make_door)(serial="push-unauth")
    comm = await ws.open_device("door", "push-unauth")

    await ws.aget(door.sync)()
    assert await comm.receive_nothing(timeout=0.5)

    handshake = await ws.authenticate(comm, device_api_key)
    assert handshake["ack"] == {"authorised": True}
    assert "sync" in handshake["by_command"]
    await comm.disconnect()


# --------------------------------------------------------------------------
# Interlock sessions: unknown and foreign ids
# --------------------------------------------------------------------------


@pytest.fixture
def interlock_pair(make_interlock, make_member):
    def _build():
        a = make_interlock(serial="edge-int-a", cost_per_session=500)
        b = make_interlock(serial="edge-int-b", cost_per_session=0)
        member = make_member(state="active", rfid="EDGE-TAG")
        member.interlocks.add(a)
        member.interlocks.add(b)
        return a, b, member

    return _build


@pytest.mark.parametrize("command", ["interlock_session_update", "interlock_session_end"])
@pytest.mark.parametrize(
    "session_id", ["11111111-1111-4111-8111-111111111111", "not-a-uuid", None]
)
@pytest.mark.protocol
@pytest.mark.django_db(transaction=True)
async def test_naming_a_session_that_does_not_exist_is_a_clean_failure(
    make_interlock, device_api_key, command, session_id
):
    """Used to kill the connection: ``InterlockLog.objects.get`` was unchecked.

    A device that retries after a restart, or sends a corrupt id, got
    ``{"command": "error"}`` and the consumer then re-raised, closing the
    socket, so the firmware had to reconnect and re-authenticate for what
    should have been a ``success: false`` reply, as an already-ended session
    gets. It now gets one, and the socket stays open.
    """
    await ws.aget(make_interlock)(serial="edge-unknown-session")
    comm, _ = await ws.open_authenticated(
        "interlock", "edge-unknown-session", device_api_key
    )

    await comm.send_json_to({"command": command, "session_id": session_id})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": command,
        "success": False,
        "reason": "session_not_found",
    }
    await comm.send_json_to({"command": "ping"})
    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {"command": "pong"}
    await comm.disconnect()


@pytest.mark.parametrize("command", ["interlock_session_update", "interlock_session_end"])
@pytest.mark.protocol
@pytest.mark.django_db(transaction=True)
async def test_a_device_cannot_touch_another_interlocks_session(
    interlock_pair, device_api_key, command
):
    """Used to succeed: the session id was not checked against the device.

    Interlock B could end the session a member started on interlock A, naming
    it by id, and the member was billed A's rate. Device keys are global (see
    ``test_device_connection.py``), so any authenticated device, or anyone
    holding the one key, could end any session in the space. A session may now
    only be updated or ended by the interlock it ran on.
    """
    from datetime import timedelta

    from django.utils import timezone

    from access.models import InterlockLog
    from memberbucks.models import MemberBucks

    a, b, member = await ws.aget(interlock_pair)()
    comm_a, _ = await ws.open_authenticated("interlock", "edge-int-a", device_api_key)
    await comm_a.send_json_to(
        {"command": "interlock_session_start", "card_id": "EDGE-TAG"}
    )
    session_id = (await comm_a.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    def _backdate():
        InterlockLog.objects.filter(id=session_id).update(
            date_started=timezone.now() - timedelta(seconds=120)
        )

    await ws.aget(_backdate)()

    comm_b, _ = await ws.open_authenticated("interlock", "edge-int-b", device_api_key)
    await comm_b.send_json_to(
        {"command": command, "session_id": session_id, "card_id": "EDGE-TAG"}
    )
    reply = await comm_b.receive_json_from(timeout=ws.TIMEOUT)

    assert reply == {"command": command, "success": False, "reason": "session_not_found"}
    session = await ws.aget(InterlockLog.objects.get)(id=session_id)
    assert session.date_ended is None
    assert await ws.aget(MemberBucks.objects.filter(user=member.user).count)() == 0

    # The interlock it ran on can still end it, and the member is billed.
    await comm_a.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "EDGE-TAG",
        }
    )
    assert (await comm_a.receive_json_from(timeout=ws.TIMEOUT))["success"] is True
    assert await ws.aget(MemberBucks.objects.filter(user=member.user).count)() == 1
    await comm_a.disconnect()
    await comm_b.disconnect()


# --------------------------------------------------------------------------
# Memberbucks: an unknown product
# --------------------------------------------------------------------------


@pytest.mark.protocol
@pytest.mark.django_db(transaction=True)
async def test_a_purchase_of_an_unknown_product_is_refused_and_charges_nothing(
    make_memberbucks_device, make_member, device_api_key
):
    from datetime import timedelta

    from django.utils import timezone

    from memberbucks.models import MemberBucks

    def _setup():
        make_memberbucks_device(serial="edge-vend")
        profile = make_member(state="active", rfid="EDGE-VEND")
        MemberBucks.objects.create(
            user=profile.user, amount=20, transaction_type="cash", description="float"
        )
        type(profile).objects.filter(pk=profile.pk).update(
            last_memberbucks_purchase=timezone.now() - timedelta(seconds=60)
        )
        return profile

    profile = await ws.aget(_setup)()
    comm, _ = await ws.open_authenticated("memberbucks", "edge-vend", device_api_key)

    await comm.send_json_to(
        {
            "command": "debit",
            "card_id": "EDGE-VEND",
            "amount": 150,
            "product_external_id": "no-such-product",
        }
    )

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "debit",
        "reason": "invalid_product_external_id",
        "success": False,
    }
    count = await ws.aget(MemberBucks.objects.filter(user=profile.user).count)()
    assert count == 1
    await comm.disconnect()


# --------------------------------------------------------------------------
# Plain (synchronous) tests
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_status_endpoint_counts_locked_out_devices(as_admin, make_door):
    door = make_door(serial="status-locked")
    door.locked_out = True
    door.save()
    make_door(serial="status-open")

    body = as_admin().get("/api/access/status/").json()

    assert sorted(d["lockedOut"] for d in body["doors"]) == [False, True]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "log_type, rejected",
    [
        ("activated", False),
        ("left_on", False),
        ("deactivated", False),
        ("rejected", True),
        ("locked_out", True),
        ("not_signed_in", True),
    ],
)
def test_only_the_rejection_kinds_of_access_log_leave_a_failed_session(
    make_interlock, make_member, log_type, rejected
):
    from access.models import InterlockLog

    interlock = make_interlock(serial="edge-log")
    member = make_member(state="active", rfid="EDGE-LOG")

    assert interlock.log_access(member.user, log_type) is True

    failed = InterlockLog.objects.filter(interlock=interlock, success=False)
    assert failed.exists() is rejected
    if rejected:
        assert failed.get().reason == log_type


@pytest.mark.django_db
def test_a_rejection_closes_any_session_still_open_on_the_interlock(
    make_interlock, make_member
):
    from access.models import InterlockLog

    interlock = make_interlock(serial="edge-reject")
    member = make_member(state="active", rfid="EDGE-REJ")
    open_session = interlock.session_start(member.user)

    interlock.session_rejected(member.user, "rejected")

    open_session.refresh_from_db()
    assert open_session.date_ended is not None
    assert InterlockLog.objects.filter(interlock=interlock).count() == 2


@pytest.mark.django_db
def test_the_log_models_describe_themselves(make_door, make_interlock, make_member):
    from access.models import DoorLog

    door = make_door(serial="edge-str-door")
    interlock = make_interlock(serial="edge-str-int")
    member = make_member(state="active", rfid="EDGE-STR")
    swipe = DoorLog.objects.create(user=member.user, door=door, success=False)
    session = interlock.session_start(member.user)

    assert "swiped at Test Door edge-str-door unsuccessfully" in str(swipe)
    assert "swiped at Test Interlock edge-str-int successfully for 0 mins" in str(session)
    assert str(door) == door.name


# --------------------------------------------------------------------------
# The Celery heartbeat
# --------------------------------------------------------------------------


def test_the_heartbeat_task_runs_and_is_scheduled_every_thirty_seconds_outside_production(
    monkeypatch,
):
    from api_access import tasks

    added = []
    sender = types.SimpleNamespace(
        add_periodic_task=lambda interval, signature, **kwargs: added.append(
            (interval, kwargs["name"])
        )
    )
    monkeypatch.delenv("MM_ENV", raising=False)

    tasks.setup_periodic_tasks(sender)
    monkeypatch.setenv("MM_ENV", "Production")
    tasks.setup_periodic_tasks(sender)

    assert added == [(30, "celery_heartbeat_logger"), (3600, "celery_heartbeat_logger")]
    assert tasks.heartbeat_logger() is None
