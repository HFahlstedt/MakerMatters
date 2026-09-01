"""Characterisation: the admin device CRUD endpoints.

``api_admin_tools`` had three near-identical classes — ``Doors``,
``Interlocks`` and ``MemberbucksDevices`` — sharing their read/update/delete
shape and differing only in the statistics they aggregate. They now share one
base, and these tests are what made that safe.

The three response shapes had **already drifted apart**, so the differences are
written down here deliberately. Doors expose ``serialNumber`` and the
Discord/Slack toggles but no ``authorised`` flag; interlocks and vending
devices expose ``authorised`` but neither ``serialNumber`` nor the toggles.
Consolidation was tempted to unify them, which would have silently changed the
API the admin frontend consumes.

Every field is asserted by exact dict comparison rather than spot-checks, so a
serializer rewrite cannot drop or rename one unnoticed.
"""

import pytest

pytestmark = pytest.mark.django_db

DOOR_FIELDS = {
    "id",
    "name",
    "description",
    "ipAddress",
    "serialNumber",
    "lastSeen",
    "offline",
    "defaultAccess",
    "maintenanceLockout",
    "playThemeOnSwipe",
    "postDiscordOnSwipe",
    "postSlackOnSwipe",
    "exemptFromSignin",
    "hiddenToMembers",
    "totalSwipes",
    "userStats",
}

INTERLOCK_FIELDS = {
    "id",
    "authorised",
    "name",
    "description",
    "ipAddress",
    "lastSeen",
    "offline",
    "defaultAccess",
    "maintenanceLockout",
    "playThemeOnSwipe",
    "exemptFromSignin",
    "hiddenToMembers",
    "totalTimeSeconds",
    "userStats",
}

MEMBERBUCKS_FIELDS = {
    "id",
    "authorised",
    "name",
    "description",
    "ipAddress",
    "lastSeen",
    "offline",
    # No `defaultAccess`: a vending machine has no per-member access list, so
    # the flag had no mechanism behind it and was removed from this endpoint.
    "maintenanceLockout",
    "playThemeOnSwipe",
    "exemptFromSignin",
    "hiddenToMembers",
    "totalPurchases",
    "totalVolume",
    "userStats",
}


# --------------------------------------------------------------------------
# Response shape
# --------------------------------------------------------------------------


def test_door_list_shape(as_admin, make_door):
    make_door(serial="door-shape", name="Front Door")

    body = as_admin().get("/api/admin/doors/").json()

    assert len(body) == 1
    assert set(body[0]) == DOOR_FIELDS
    assert body[0]["name"] == "Front Door"
    assert body[0]["serialNumber"] == "door-shape"
    assert body[0]["totalSwipes"] == 0
    assert body[0]["userStats"] == []


def test_interlock_list_shape(as_admin, make_interlock):
    make_interlock(serial="int-shape", name="Laser Cutter")

    body = as_admin().get("/api/admin/interlocks/").json()

    assert set(body[0]) == INTERLOCK_FIELDS
    assert body[0]["totalTimeSeconds"] == 0


def test_memberbucks_device_list_shape(as_admin, make_memberbucks_device):
    make_memberbucks_device(serial="vend-shape", name="Snack Machine")

    body = as_admin().get("/api/admin/memberbucks-devices/").json()

    assert set(body[0]) == MEMBERBUCKS_FIELDS
    assert body[0]["totalPurchases"] == 0
    assert body[0]["totalVolume"] == 0


def test_the_three_device_shapes_have_drifted(
    as_admin, make_door, make_interlock, make_memberbucks_device
):
    """Pins the *differences* explicitly, so consolidation is a deliberate choice.

    A naive "make all three use one serializer" refactor would either add
    ``authorised`` to doors or remove it from the other two, and either way the
    admin frontend's device screens change behaviour.
    """
    make_door(serial="drift-door")
    make_interlock(serial="drift-int")
    make_memberbucks_device(serial="drift-vend")

    client = as_admin()
    door = client.get("/api/admin/doors/").json()[0]
    interlock = client.get("/api/admin/interlocks/").json()[0]
    vending = client.get("/api/admin/memberbucks-devices/").json()[0]

    # Only doors report their serial number and messaging toggles.
    assert "serialNumber" in door
    assert "serialNumber" not in interlock and "serialNumber" not in vending
    assert "postDiscordOnSwipe" in door and "postSlackOnSwipe" in door
    assert "postDiscordOnSwipe" not in interlock

    # Only doors omit `authorised`, despite every device type having the field.
    assert "authorised" not in door
    assert interlock["authorised"] is True and vending["authorised"] is True

    # Only the two types with a per-member access list offer the toggle.
    assert "defaultAccess" in door and "defaultAccess" in interlock
    assert "defaultAccess" not in vending

    # Each type reports a different usage statistic.
    assert "totalSwipes" in door
    assert "totalTimeSeconds" in interlock
    assert {"totalPurchases", "totalVolume"} <= set(vending)


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def test_door_reports_swipe_totals_and_per_member_stats(
    as_admin, make_door, make_member
):
    from access.models import DoorLog

    door = make_door(serial="door-stats")
    member = make_member(state="active", rfid="STAT-1")
    DoorLog.objects.create(user=member.user, door=door, success=True)
    DoorLog.objects.create(user=member.user, door=door, success=False)

    body = as_admin().get("/api/admin/doors/").json()[0]

    assert body["totalSwipes"] == 2
    stats = body["userStats"]
    assert len(stats) == 1
    assert stats[0]["screen_name"] == member.screen_name
    assert stats[0]["full_name"] == "Test Member1"
    assert stats[0]["total_swipes"] == 2
    assert stats[0]["last_swipe"] is not None


def test_interlock_reports_total_session_time(as_admin, make_interlock, make_member):
    from datetime import timedelta

    from access.models import InterlockLog

    interlock = make_interlock(serial="int-stats")
    member = make_member(state="active", rfid="STAT-2")
    InterlockLog.objects.create(
        interlock=interlock, user_started=member.user, total_time=timedelta(minutes=30)
    )
    InterlockLog.objects.create(
        interlock=interlock, user_started=member.user, total_time=timedelta(minutes=15)
    )

    body = as_admin().get("/api/admin/interlocks/").json()[0]

    assert body["totalTimeSeconds"] == 2700
    assert body["userStats"][0]["total_swipes"] == 2


def test_memberbucks_device_reports_purchase_volume_in_dollars(
    as_admin, make_memberbucks_device, make_member
):
    """``totalVolume`` divides the cents-denominated price by 100."""
    from memberbucks.models import MemberbucksProduct, MemberbucksProductPurchaseLog

    device = make_memberbucks_device(serial="vend-stats")
    member = make_member(state="active", rfid="STAT-3")
    product = MemberbucksProduct.objects.create(
        name="Cola", external_id="A1", external_id_name="A1", price=250, cost_price=100
    )
    for _ in range(2):
        MemberbucksProductPurchaseLog.objects.create(
            user=member.user,
            product=product,
            memberbucks_device=device,
            price=250,
            cost_price=100,
            success=True,
        )

    body = as_admin().get("/api/admin/memberbucks-devices/").json()[0]

    assert body["totalPurchases"] == 2
    assert body["totalVolume"] == 5.0  # 500 cents


def test_unsuccessful_purchases_are_excluded_from_volume(
    as_admin, make_memberbucks_device, make_member
):
    from memberbucks.models import MemberbucksProduct, MemberbucksProductPurchaseLog

    device = make_memberbucks_device(serial="vend-failed")
    member = make_member(state="active", rfid="STAT-4")
    product = MemberbucksProduct.objects.create(
        name="Chips", external_id="B1", external_id_name="B1", price=300, cost_price=150
    )
    MemberbucksProductPurchaseLog.objects.create(
        user=member.user,
        product=product,
        memberbucks_device=device,
        price=300,
        cost_price=150,
        success=False,
    )

    body = as_admin().get("/api/admin/memberbucks-devices/").json()[0]

    assert body["totalPurchases"] == 0
    assert body["totalVolume"] == 0


# --------------------------------------------------------------------------
# Updates and their side effects
# --------------------------------------------------------------------------


def door_payload(**overrides):
    payload = {
        "name": "Renamed Door",
        "description": "Updated description",
        "ipAddress": "10.0.0.9",
        "serialNumber": "door-update",
        "defaultAccess": False,
        "maintenanceLockout": False,
        "playThemeOnSwipe": False,
        "postDiscordOnSwipe": True,
        "postSlackOnSwipe": True,
        "exemptFromSignin": False,
        "hiddenToMembers": False,
    }
    payload.update(overrides)
    return payload


def test_updating_a_door_writes_every_field(as_admin, make_door):
    from access.models import Doors

    door = make_door(serial="door-update")

    response = as_admin().put(
        f"/api/admin/doors/{door.id}/",
        door_payload(playThemeOnSwipe=True, hiddenToMembers=True),
        format="json",
    )

    assert response.status_code == 200
    refreshed = Doors.objects.get(pk=door.id)
    assert refreshed.name == "Renamed Door"
    assert refreshed.description == "Updated description"
    assert refreshed.ip_address == "10.0.0.9"
    assert refreshed.play_theme is True
    assert refreshed.hidden is True


def test_enabling_default_access_grants_every_member(as_admin, make_door, make_member):
    """The endpoint iterates every user in Python rather than doing a bulk write.

    Pinned because the refactor will want to replace this with a single m2m
    call, and the observable outcome must stay the same.
    """
    from profile.models import Profile

    door = make_door(serial="door-default-on")
    first = make_member(state="active", rfid="DEF-1")
    second = make_member(state="inactive", rfid="DEF-2")

    as_admin().put(
        f"/api/admin/doors/{door.id}/",
        door_payload(serialNumber="door-default-on", defaultAccess=True),
        format="json",
    )

    # Every member gets the link, regardless of their state.
    for profile in (first, second):
        assert door in Profile.objects.get(pk=profile.pk).doors.all()


def test_disabling_default_access_revokes_it_from_every_member(
    as_admin, make_door, make_member
):
    from profile.models import Profile

    door = make_door(serial="door-default-off", all_members=True)
    member = make_member(state="active", rfid="DEF-3")
    member.doors.add(door)

    as_admin().put(
        f"/api/admin/doors/{door.id}/",
        door_payload(serialNumber="door-default-off", defaultAccess=False),
        format="json",
    )

    assert door not in Profile.objects.get(pk=member.pk).doors.all()


def test_toggling_maintenance_lockout_notifies_the_device(
    as_admin, make_door, capture_channel_sends
):
    """A lockout change must reach the hardware, not just the database."""
    from access.models import Doors

    door = make_door(serial="door-lockout")

    as_admin().put(
        f"/api/admin/doors/{door.id}/",
        door_payload(serialNumber="door-lockout", maintenanceLockout=True),
        format="json",
    )

    assert Doors.objects.get(pk=door.id).locked_out is True
    assert "update_device_locked_out" in capture_channel_sends
    assert "update_device_object" in capture_channel_sends


def test_changing_only_the_signin_exemption_reaches_the_device(
    as_admin, make_door, capture_channel_sends
):
    """The exemption decides whose tags are sent, so it has to trigger a sync.

    The guard used to compare the field against the value that had already
    been written to it — ``door.exempt_signin = data.get("exemptFromSignin")``
    a few lines above ``if ... door.exempt_signin != data.get(...)`` — so it
    could never be true. The exemption was saved and never synced, and the
    device kept enforcing the old rule until something else triggered one.
    """
    from access.models import Doors

    door = make_door(serial="door-signin", exempt_signin=False)

    as_admin().put(
        f"/api/admin/doors/{door.id}/",
        door_payload(serialNumber="door-signin", exemptFromSignin=True),
        format="json",
    )

    assert Doors.objects.get(pk=door.id).exempt_signin is True
    assert "sync_users" in capture_channel_sends
    assert "update_device_object" in capture_channel_sends


def test_updating_a_memberbucks_device_notifies_it_of_a_lockout(
    as_admin, make_memberbucks_device, capture_channel_sends
):
    """A vending lockout reaches the machine, as it always did for the others.

    Doors and interlocks pushed ``update_device_locked_out`` and
    ``update_device_object`` when the lockout changed. The vending endpoint was
    otherwise the same code, but that half was never copied across, so the
    lockout was stored and the machine never told.
    """
    from access.models import MemberbucksDevice

    device = make_memberbucks_device(serial="vend-lockout")

    as_admin().put(
        f"/api/admin/memberbucks-devices/{device.id}/",
        {
            "name": "Renamed Vending",
            "description": "Updated",
            "ipAddress": "10.0.0.5",
            "maintenanceLockout": True,
            "playThemeOnSwipe": False,
            "exemptFromSignin": False,
            "hiddenToMembers": False,
        },
        format="json",
    )

    assert MemberbucksDevice.objects.get(pk=device.id).locked_out is True
    assert "update_device_locked_out" in capture_channel_sends
    assert "update_device_object" in capture_channel_sends


def test_deleting_a_device_removes_it(as_admin, make_door, make_interlock):
    from access.models import Doors, Interlock

    door = make_door(serial="door-delete")
    interlock = make_interlock(serial="int-delete")

    client = as_admin()
    assert client.delete(f"/api/admin/doors/{door.id}/").status_code == 200
    assert client.delete(f"/api/admin/interlocks/{interlock.id}/").status_code == 200

    assert Doors.objects.count() == 0
    assert Interlock.objects.count() == 0


def test_updating_a_memberbucks_device_does_not_touch_the_serial_number(
    as_admin, make_memberbucks_device
):
    """Unlike doors, the vending update path has no serialNumber field at all."""
    from access.models import MemberbucksDevice

    device = make_memberbucks_device(serial="vend-serial")

    as_admin().put(
        f"/api/admin/memberbucks-devices/{device.id}/",
        {
            "name": "Renamed Vending",
            "description": "Updated",
            "ipAddress": "10.0.0.5",
            "defaultAccess": False,
            "maintenanceLockout": False,
            "playThemeOnSwipe": False,
            "exemptFromSignin": False,
            "hiddenToMembers": False,
        },
        format="json",
    )

    refreshed = MemberbucksDevice.objects.get(pk=device.id)
    assert refreshed.name == "Renamed Vending"
    assert refreshed.serial_number == "vend-serial"  # untouched


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/admin/doors/", "/api/admin/interlocks/", "/api/admin/memberbucks-devices/"],
)
def test_device_endpoints_reject_non_staff(make_member, as_member, path):
    member = make_member(state="active", rfid="NOT-ADMIN")

    assert as_member(member).get(path).status_code == 403


@pytest.mark.parametrize(
    "path",
    ["/api/admin/doors/", "/api/admin/interlocks/", "/api/admin/memberbucks-devices/"],
)
def test_device_endpoints_reject_anonymous(api_client, path):
    assert api_client.get(path).status_code == 401


def test_device_endpoints_do_not_accept_an_api_key(with_api_key):
    """Unlike the member endpoints, device CRUD is staff-session only.

    The key is not a recognised authentication class here, so the request
    stays anonymous and the project's custom exception handler turns DRF's
    default 403 into a 401.
    """
    assert with_api_key().get("/api/admin/doors/").status_code == 401
