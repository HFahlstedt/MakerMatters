"""Characterisation: what actually differs between the three device types.

``AccessControlledDevice`` is the base class for ``Doors``, ``Interlock`` and
``MemberbucksDevice``. It used not to delegate to them: it switched on
``self.type`` to decide what its own subclasses do, in two methods —
``log_event()`` chose an EventLog type and foreign key, ``get_tags()`` chose how
to narrow the authorised profiles. Each now sits on the class it describes.

These tests were written against the switch and pass unchanged against the
polymorphic version, which is the point of them: they describe the behaviour
from the outside and never name the mechanism. They deliberately cover the
``MemberbucksDevice`` paths no other test reaches, because that is the type
whose declared configuration and actual behaviour disagree — see
``test_a_vending_machine_ignores_its_default_access_flag``.
"""

import pytest

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# get_tags: who is authorised, per device type
# --------------------------------------------------------------------------


def test_a_door_authorises_only_the_members_linked_to_it(make_door, make_member):
    door = make_door(serial="hier-door")
    permitted = make_member(state="active", rfid="HIER-DOOR-YES")
    make_member(state="active", rfid="HIER-DOOR-NO")
    permitted.doors.add(door)

    tags, _hash = door.get_tags()

    assert tags == ["HIER-DOOR-YES"]


def test_an_interlock_authorises_only_the_members_linked_to_it(
    make_interlock, make_member
):
    interlock = make_interlock(serial="hier-int")
    permitted = make_member(state="active", rfid="HIER-INT-YES")
    make_member(state="active", rfid="HIER-INT-NO")
    permitted.interlocks.add(interlock)

    tags, _hash = interlock.get_tags()

    assert tags == ["HIER-INT-YES"]


def test_a_vending_machine_authorises_every_active_member(
    make_memberbucks_device, make_member
):
    """No link table is consulted: money, not permission, gates a purchase."""
    device = make_memberbucks_device(serial="hier-vend")
    make_member(state="active", rfid="HIER-VEND-1")
    make_member(state="active", rfid="HIER-VEND-2")

    tags, _hash = device.get_tags()

    assert sorted(tags) == ["HIER-VEND-1", "HIER-VEND-2"]


def test_a_vending_machine_ignores_its_default_access_flag(
    make_memberbucks_device, make_member
):
    """DEFECT, pinned: ``all_members`` is settable but inert on this type.

    ``MemberbucksDevice`` used to declare ``all_members = True`` in its class
    body, which never took effect — Django writes the field's stored value into
    the instance during ``Model.__init__``, so the instance attribute always
    shadowed the class attribute. The behaviour it was meant to express is now
    stated by ``MemberbucksDevice.get_authorised_profiles``, which returns
    every active member regardless of the flag.

    The admin API nonetheless reads and writes this field for vending machines
    (``api_admin_tools/views.py`` — ``defaultAccess``), so the admin screen
    offers a toggle that does nothing in either position. Both positions are
    pinned here.
    """
    device = make_memberbucks_device(serial="hier-vend-flag", all_members=False)
    make_member(state="active", rfid="HIER-FLAG-1")

    assert device.all_members is False
    assert device.get_tags()[0] == ["HIER-FLAG-1"]

    device.all_members = True
    device.save()

    assert device.get_tags()[0] == ["HIER-FLAG-1"]


def test_the_base_class_refuses_to_produce_tags(make_member):
    """A device with no way to answer is a hard error, not an open door."""
    from access.models import AccessControlledDevice

    make_member(state="active", rfid="HIER-BASE")
    device = AccessControlledDevice(name="Bare", description="No subclass")

    with pytest.raises(Exception, match="Unknown device type"):
        device.get_tags()


@pytest.mark.parametrize(
    "kind", ["make_door", "make_interlock", "make_memberbucks_device"]
)
def test_every_type_excludes_members_without_a_tag_or_without_active_state(
    request, make_member, kind
):
    """The two universal filters, applied before any per-type narrowing."""
    device = request.getfixturevalue(kind)(serial=f"hier-filter-{kind}")

    tagged_but_inactive = make_member(state="inactive", rfid="HIER-INACTIVE")
    active_but_untagged = make_member(state="active", rfid=None)
    permitted = make_member(state="active", rfid="HIER-OK")
    for member in (tagged_but_inactive, active_but_untagged, permitted):
        member.doors.add(device) if kind == "make_door" else None
        member.interlocks.add(device) if kind == "make_interlock" else None

    tags, _hash = device.get_tags()

    assert tags == ["HIER-OK"]


def test_site_sign_in_gating_applies_to_every_type(
    request, make_memberbucks_device, make_member, set_config
):
    """With sign-in enforced, a member who is not signed in loses their tag."""
    set_config(ENABLE_PORTAL_SITE_SIGN_IN=True)
    device = make_memberbucks_device(serial="hier-signin")
    make_member(state="active", rfid="HIER-SIGNIN")

    assert device.get_tags()[0] == []

    device.exempt_signin = True
    device.save()

    assert device.get_tags()[0] == ["HIER-SIGNIN"]


# --------------------------------------------------------------------------
# log_event: which EventLog column points back at the device
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,expected_logtype,expected_column",
    [
        ("make_door", "door", "door"),
        ("make_interlock", "interlock", "interlock"),
        ("make_memberbucks_device", "memberbucksdevice", "memberbucks_device"),
    ],
)
def test_log_event_records_the_type_and_back_reference(
    request, kind, expected_logtype, expected_column
):
    from profile.models import EventLog

    device = request.getfixturevalue(kind)(serial=f"hier-log-{kind}")

    assert device.log_event(description="Hello", data="payload") is True

    entry = EventLog.objects.get(description="Hello")
    assert entry.logtype == expected_logtype
    assert entry.data == "payload"
    assert getattr(entry, expected_column) == device
    # The other two back-references stay empty.
    for column in {"door", "interlock", "memberbucks_device"} - {expected_column}:
        assert getattr(entry, column) is None


def test_the_base_class_logs_nothing_and_reports_nothing(db):
    """Unlike get_tags, an undeclared device logs nothing and says so quietly."""
    from access.models import AccessControlledDevice
    from profile.models import EventLog

    device = AccessControlledDevice(name="Bare log", description="No subclass")

    assert device.log_event(description="Dropped") is None
    assert not EventLog.objects.filter(description="Dropped").exists()


@pytest.mark.parametrize(
    "kind", ["make_door", "make_interlock", "make_memberbucks_device"]
)
def test_the_lifecycle_helpers_all_route_through_log_event(request, kind):
    """The nine ``log_*`` wrappers exist on every type and share one path."""
    from profile.models import EventLog

    device = request.getfixturevalue(kind)(serial=f"hier-life-{kind}")

    device.log_connected()
    device.log_disconnected()
    device.log_authenticated()
    device.log_force_rebooted()
    device.log_force_sync()
    device.log_force_bump()
    device.log_force_lock()
    device.log_force_unlock()

    descriptions = list(
        EventLog.objects.order_by("id").values_list("description", flat=True)
    )
    assert descriptions == [
        "Device connected.",
        "Device disconnected.",
        "Device authenticated.",
        "Device manually rebooted.",
        "Device manually synced.",
        "Device manually bumped.",
        "Device manually locked.",
        "Device manually unlocked.",
    ]


# --------------------------------------------------------------------------
# The type string itself
# --------------------------------------------------------------------------


def test_the_type_string_is_stable_and_reaches_the_metrics_labels(
    make_door, make_interlock, make_memberbucks_device
):
    """``type`` is a metrics dimension as well as a switch, so it is a contract.

    Changing these strings renames Prometheus label values and breaks any
    existing dashboard, independently of whatever the code does with them.
    """
    door = make_door(serial="hier-type-door")
    interlock = make_interlock(serial="hier-type-int")
    vending = make_memberbucks_device(serial="hier-type-vend")

    assert (door.type, interlock.type, vending.type) == (
        "door",
        "interlock",
        "memberbucks",
    )
    assert door.get_metrics_labels() == {
        "type": "door",
        "id": door.id,
        "name": door.name,
    }
    assert interlock.get_metrics_labels()["type"] == "interlock"
    assert vending.get_metrics_labels()["type"] == "memberbucks"
