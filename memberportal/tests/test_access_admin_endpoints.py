"""Characterisation: the admin-facing HTTP endpoints in ``api_access``.

These sit apart from ``api_admin_tools`` but are part of the same admin
surface: granting and revoking device access, and the remote control commands
(sync, reboot, bump, lock, unlock).

Two things make them a refactor target. ``LockDevice`` and ``UnlockDevice`` are
duplicate classes differing only in which model method they call, and the whole
group is inconsistent about what it returns, because the underlying model
methods disagree about whether to return anything at all.

Three of these endpoints are reachable with an external API key, which is the
only part of the system a third party can drive. That path is exercised here
explicitly.
"""

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def external_key_client(api_client):
    """Authenticate with an ``ExternalAccessControlAPIKey``.

    This is a different key table from the generic DRF one used elsewhere; only
    the access-control endpoints accept it.
    """
    from access.models import ExternalAccessControlAPIKey

    def _auth():
        _obj, raw = ExternalAccessControlAPIKey.objects.create_key(name="external")
        api_client.credentials(HTTP_AUTHORIZATION=f"Api-Key {raw}")
        return api_client

    return _auth


@pytest.fixture
def capture_channel_sends(monkeypatch):
    """Record what would be pushed to devices, without a channel layer."""
    sent = []
    monkeypatch.setattr(
        "access.models.async_to_sync",
        lambda fn: (lambda *args, **kwargs: sent.append(args[1]["type"])),
    )
    return sent


# --------------------------------------------------------------------------
# System status
# --------------------------------------------------------------------------


def test_status_shape(as_admin, make_door, make_interlock, make_memberbucks_device):
    make_door(serial="status-door")
    make_interlock(serial="status-int")
    make_memberbucks_device(serial="status-vend")

    body = as_admin().get("/api/access/status/").json()

    assert set(body) == {"doors", "interlocks", "memberbucksDevices"}
    assert set(body["doors"][0]) == {"id", "name", "lastSeen", "lockedOut", "offline"}
    assert body["doors"][0]["offline"] is False


def test_status_is_readable_with_an_external_api_key(external_key_client, make_door):
    make_door(serial="status-key")

    assert external_key_client().get("/api/access/status/").status_code == 200


def test_status_can_fail_loudly_when_a_device_is_offline(as_admin, make_door):
    """``?errorIfOffline`` turns the endpoint into an uptime check."""
    from datetime import timedelta

    from django.utils import timezone

    from access.models import Doors

    door = make_door(serial="status-offline", report_online_status=True)
    Doors.objects.filter(pk=door.id).update(
        last_seen=timezone.now() - timedelta(minutes=10)
    )

    client = as_admin()
    assert client.get("/api/access/status/").status_code == 200
    assert client.get("/api/access/status/?errorIfOffline=1").status_code == 503


def test_a_device_excluded_from_reporting_does_not_fail_the_check(as_admin, make_door):
    from datetime import timedelta

    from django.utils import timezone

    from access.models import Doors

    door = make_door(serial="status-excluded", report_online_status=False)
    Doors.objects.filter(pk=door.id).update(
        last_seen=timezone.now() - timedelta(minutes=10)
    )

    response = as_admin().get("/api/access/status/?errorIfOffline=1")

    assert response.status_code == 200
    assert response.json()["doors"][0]["offline"] is True


# --------------------------------------------------------------------------
# Granting and revoking
# --------------------------------------------------------------------------


def test_authorising_a_door_grants_access_and_syncs(
    as_admin, make_member, make_door, capture_channel_sends
):
    from profile.models import Profile

    door = make_door(serial="grant-door")
    member = make_member(state="active", rfid="GRANT-1")

    response = as_admin().put(
        f"/api/access/doors/{door.id}/authorise/{member.user_id}/"
    )

    assert response.status_code == 200
    assert door in Profile.objects.get(pk=member.pk).doors.all()
    assert "sync_users" in capture_channel_sends


def test_revoking_a_door_removes_access_and_syncs(
    as_admin, make_member, make_door, capture_channel_sends
):
    from profile.models import Profile

    door = make_door(serial="revoke-door")
    member = make_member(state="active", rfid="REVOKE-1")
    member.doors.add(door)

    as_admin().put(f"/api/access/doors/{door.id}/revoke/{member.user_id}/")

    assert door not in Profile.objects.get(pk=member.pk).doors.all()
    assert "sync_users" in capture_channel_sends


def test_authorising_and_revoking_an_interlock(
    as_admin, make_member, make_interlock, capture_channel_sends
):
    from profile.models import Profile

    interlock = make_interlock(serial="grant-int")
    member = make_member(state="active", rfid="GRANT-2")
    client = as_admin()

    client.put(f"/api/access/interlocks/{interlock.id}/authorise/{member.user_id}/")
    assert interlock in Profile.objects.get(pk=member.pk).interlocks.all()

    client.put(f"/api/access/interlocks/{interlock.id}/revoke/{member.user_id}/")
    assert interlock not in Profile.objects.get(pk=member.pk).interlocks.all()


def test_granting_access_is_staff_only(make_member, as_member, make_door):
    door = make_door(serial="grant-denied")
    member = make_member(state="active", rfid="GRANT-3")

    response = as_member(member).put(
        f"/api/access/doors/{door.id}/authorise/{member.user_id}/"
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------
# Remote control
# --------------------------------------------------------------------------


def test_syncing_a_door(as_admin, make_door, capture_channel_sends):
    door = make_door(serial="cmd-sync")

    body = as_admin().post(f"/api/access/doors/{door.id}/sync/").json()

    assert body == {"success": True}
    assert "sync_users" in capture_channel_sends


def test_there_is_no_sync_route_for_interlocks(as_admin, make_interlock):
    """DEFECT, pinned: the admin frontend calls this URL for every device type.

    ``AdminTools/DeviceDialog.vue`` builds the path from the device type, so
    pressing "sync" on an interlock or vending machine requests a route that
    was never registered.
    """
    interlock = make_interlock(serial="cmd-nosync")

    assert (
        as_admin().post(f"/api/access/interlocks/{interlock.id}/sync/").status_code
        == 404
    )


@pytest.mark.parametrize("device_kind", ["doors", "interlocks"])
def test_rebooting_a_device(
    as_admin, make_door, make_interlock, capture_channel_sends, device_kind
):
    device = (
        make_door(serial=f"cmd-reboot-{device_kind}")
        if device_kind == "doors"
        else make_interlock(serial=f"cmd-reboot-{device_kind}")
    )

    body = as_admin().post(f"/api/access/{device_kind}/{device.id}/reboot/").json()

    # `success` is null here too — see the lock/unlock test below.
    assert body == {"success": None}
    assert "device_reboot" in capture_channel_sends


def test_bumping_a_door(as_admin, make_door, capture_channel_sends):
    door = make_door(serial="cmd-bump")

    body = as_admin().post(f"/api/access/doors/{door.id}/bump/").json()

    assert body == {"success": True}
    assert "door_bump" in capture_channel_sends


def test_bumping_a_door_without_a_serial_number_reports_failure(as_admin, make_door):
    door = make_door(serial=None)

    body = as_admin().post(f"/api/access/doors/{door.id}/bump/").json()

    assert body == {"success": False}


@pytest.mark.parametrize("command", ["lock", "unlock"])
@pytest.mark.parametrize("device_kind", ["doors", "interlocks"])
def test_lock_and_unlock_report_success_as_null(
    as_admin,
    make_door,
    make_interlock,
    capture_channel_sends,
    command,
    device_kind,
):
    """DEFECT, pinned: ``success`` is meaningless for most remote commands.

    Three of the five device commands have no return statement, yet every view
    wraps the result as ``{"success": <returned value>}``:

        sync()   -> True          (always, even with no serial number)
        bump()   -> True / False  (the only honest one)
        reboot() -> None
        lock()   -> None
        unlock() -> None

    So a caller cannot distinguish a lock that reached the device from one
    that silently did nothing because the device has no serial number.
    """
    serial = f"cmd-{command}-{device_kind}"
    device = (
        make_door(serial=serial)
        if device_kind == "doors"
        else make_interlock(serial=serial)
    )

    body = as_admin().post(f"/api/access/{device_kind}/{device.id}/{command}/").json()

    assert body == {"success": None}
    assert f"device_{command}" in capture_channel_sends


# --------------------------------------------------------------------------
# The externally callable subset
# --------------------------------------------------------------------------


def test_the_bump_api_is_disabled_by_default(external_key_client, make_door):
    """``ENABLE_DOOR_BUMP_API`` gates third-party access even with a valid key."""
    door = make_door(serial="ext-bump-off")

    response = external_key_client().post(f"/api/access/doors/{door.id}/bump/")

    assert response.status_code == 403
    assert response.json() == {
        "success": False,
        "error": "This API is disabled in the config.",
    }


def test_bumping_with_an_external_key_crashes_when_enabled(
    external_key_client, make_door, set_config, capture_channel_sends
):
    """DEFECT, pinned: the one endpoint built for third parties cannot be used.

    ``Doors.bump()`` treats a non-None ``request`` as proof there is a logged-in
    user and immediately reads ``request.user.profile``. An API-key request is
    anonymous, so this raises ``AttributeError``. The ``else`` branch that
    handles an unknown "system" caller is unreachable, because the view always
    passes the request through.
    """
    set_config(ENABLE_DOOR_BUMP_API=True)
    door = make_door(serial="ext-bump-on")

    with pytest.raises(AttributeError):
        external_key_client().post(f"/api/access/doors/{door.id}/bump/")


@pytest.mark.parametrize("command", ["lock", "unlock"])
def test_lock_and_unlock_with_an_external_key_also_crash(
    external_key_client, make_door, set_config, capture_channel_sends, command
):
    """Same root cause: ``request.user.log_event`` on an anonymous request."""
    set_config(ENABLE_DOOR_BUMP_API=True)
    door = make_door(serial=f"ext-{command}")

    with pytest.raises(AttributeError):
        external_key_client().post(f"/api/access/doors/{door.id}/{command}/")


def test_remote_commands_reject_anonymous_callers(api_client, make_door):
    door = make_door(serial="ext-anon")

    assert api_client.post(f"/api/access/doors/{door.id}/bump/").status_code == 401


# --------------------------------------------------------------------------
# Member-facing permissions view
# --------------------------------------------------------------------------


def test_member_permissions_shape(make_member, as_member, make_door, make_interlock):
    door = make_door(serial="perm-door")
    interlock = make_interlock(serial="perm-int")
    member = make_member(state="active", rfid="PERM-1")
    member.doors.add(door)

    body = as_member(member).get("/api/access/permissions/").json()

    assert set(body) == {"doors", "interlocks"}
    assert set(body["doors"][0]) == {"name", "access", "id", "locked_out", "offline"}
    assert body["doors"][0]["access"] is True
    assert body["interlocks"][0]["access"] is False


def test_an_inactive_member_sees_no_access(make_member, as_member, make_door):
    """State, not the permission link, is what the member-facing view reports."""
    door = make_door(serial="perm-inactive")
    member = make_member(state="inactive", rfid="PERM-2")
    member.doors.add(door)

    body = as_member(member).get("/api/access/permissions/").json()

    assert body["doors"][0]["access"] is False


# --------------------------------------------------------------------------
# Contracts that must survive consolidating the duplicated view classes
# --------------------------------------------------------------------------


def test_status_reports_counts_under_the_historical_metric_labels(
    as_admin, make_door, make_interlock, make_memberbucks_device
):
    """The vending-machine metric label is ``spacebucksDevice``.

    It predates the memberbucks rename and does not match ``device.type``,
    which is ``memberbucks``. Any Grafana dashboard in the wild queries the
    old name, so the mismatch is a contract rather than a slip to tidy up
    while collapsing the three identical loops in ``AccessSystemStatus``.
    """
    import api_access.metrics as metrics

    make_door(serial="metrics-door")
    make_interlock(serial="metrics-int")
    make_memberbucks_device(serial="metrics-vend")

    as_admin().get("/api/access/status/")

    for label in ("door", "interlock", "spacebucksDevice"):
        assert metrics.devices_total.labels(type=label)._value.get() == 1
        assert metrics.devices_online_total.labels(type=label)._value.get() == 1
        assert metrics.devices_offline_total.labels(type=label)._value.get() == 0
        assert metrics.devices_locked_out_total.labels(type=label)._value.get() == 0


@pytest.mark.parametrize("device_kind", ["doors", "interlocks"])
def test_rebooting_records_which_admin_asked_for_it(
    as_admin, make_door, make_interlock, capture_channel_sends, device_kind
):
    """Every remote command names the admin who sent it.

    This did not hold before the command views were consolidated: of the two
    otherwise identical reboot views, only the door one passed the request
    through to the model, so an interlock reboot left no trace of who ordered
    it. Both types now share one view and one code path.
    """
    from profile.models import UserEventLog

    device = (
        make_door(serial=f"audit-reboot-{device_kind}")
        if device_kind == "doors"
        else make_interlock(serial=f"audit-reboot-{device_kind}")
    )
    client = as_admin()

    client.post(f"/api/access/{device_kind}/{device.id}/reboot/")

    entry = UserEventLog.objects.get(user=client.admin_profile.user)
    assert entry.logtype == "admin"
    assert entry.description.startswith("Sent a reboot request to")
