"""Characterisation: the door protocol — tag sync and swipe logging.

The critical thing to understand about this system is that **the device decides**.
A door matches a card against its own locally cached tag list and opens itself,
then tells the server what it did. The server's job is to keep that cache
correct and to log outcomes.

That makes ``get_tags()`` the single most safety-relevant function in the
codebase: everything about who can physically enter the building reduces to
which tags end up in that list.
"""

import pytest

from tests import ws

pytestmark = [pytest.mark.protocol, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def door_with_member(make_door, make_member):
    """An authorised door plus one active member holding a tag and linked to it."""

    def _build(
        serial="door-sync",
        member_state="active",
        rfid="TAG-AAA",
        link=True,
        **door_kwargs,
    ):
        door = make_door(serial=serial, authorised=True, **door_kwargs)
        profile = make_member(state=member_state, rfid=rfid)
        if link:
            profile.doors.add(door)
        return door, profile

    return _build


# --------------------------------------------------------------------------
# Tag sync
# --------------------------------------------------------------------------


async def test_authentication_pushes_an_initial_tag_sync(
    door_with_member, device_api_key
):
    _door, _profile = await ws.aget(door_with_member)(serial="door-sync-01")

    comm, handshake = await ws.open_authenticated(
        "door", "door-sync-01", device_api_key
    )

    sync = handshake["by_command"]["sync"]
    assert sync["tags"] == ["TAG-AAA"]
    assert sync["hash"] == ws.expected_tag_hash(["TAG-AAA"])
    await comm.disconnect()


async def test_authentication_pushes_the_current_lockout_state(
    door_with_member, device_api_key
):
    await ws.aget(door_with_member)(serial="door-sync-lock", locked_out=True)

    comm, handshake = await ws.open_authenticated(
        "door", "door-sync-lock", device_api_key
    )

    assert handshake["by_command"]["update_device_locked_out"]["locked_out"] is True
    await comm.disconnect()


async def test_explicit_sync_command_returns_the_tag_list(
    door_with_member, device_api_key
):
    await ws.aget(door_with_member)(serial="door-sync-02")

    comm, _ = await ws.open_authenticated("door", "door-sync-02", device_api_key)
    await comm.send_json_to({"command": "sync"})

    sync = await comm.receive_json_from(timeout=ws.TIMEOUT)
    assert sync["command"] == "sync"
    assert sync["tags"] == ["TAG-AAA"]
    await comm.disconnect()


async def test_tag_hash_is_md5_of_the_python_list_repr(
    door_with_member, device_api_key
):
    """The firmware caches on this hash, so its exact construction is a contract.

    It is ``md5(str(list))`` — the repr of a Python list, including quotes and
    spaces — not a hash of JSON. Anything that changes list ordering or repr
    invalidates every device cache in the field.
    """
    door, first = await ws.aget(door_with_member)(serial="door-hash", rfid="TAG-001")

    def _add_second():
        from profile.models import Profile, User

        user = User.objects.create(email="second@example.com", email_verified=True)
        profile = Profile.objects.create(
            user=user,
            first_name="Second",
            last_name="Member",
            screen_name="second",
            state="active",
            rfid="TAG-002",
        )
        profile.doors.add(door)

    await ws.aget(_add_second)()

    comm, handshake = await ws.open_authenticated("door", "door-hash", device_api_key)
    sync = handshake["by_command"]["sync"]

    assert sorted(sync["tags"]) == ["TAG-001", "TAG-002"]
    assert sync["hash"] == ws.expected_tag_hash(sync["tags"])
    await comm.disconnect()


@pytest.mark.parametrize(
    "member_state,should_be_synced",
    [("active", True), ("inactive", False), ("noob", False), ("accountonly", False)],
)
async def test_only_active_members_are_synced(
    door_with_member, device_api_key, member_state, should_be_synced
):
    await ws.aget(door_with_member)(
        serial=f"door-state-{member_state}", member_state=member_state
    )

    comm, handshake = await ws.open_authenticated(
        "door", f"door-state-{member_state}", device_api_key
    )

    tags = handshake["by_command"]["sync"]["tags"]
    assert ("TAG-AAA" in tags) is should_be_synced
    await comm.disconnect()


async def test_members_without_a_tag_are_skipped(door_with_member, device_api_key):
    await ws.aget(door_with_member)(serial="door-no-tag", rfid=None)

    comm, handshake = await ws.open_authenticated("door", "door-no-tag", device_api_key)

    assert handshake["by_command"]["sync"]["tags"] == []
    await comm.disconnect()


async def test_members_not_linked_to_the_door_are_skipped(
    door_with_member, device_api_key
):
    await ws.aget(door_with_member)(serial="door-unlinked", link=False)

    comm, handshake = await ws.open_authenticated(
        "door", "door-unlinked", device_api_key
    )

    assert handshake["by_command"]["sync"]["tags"] == []
    await comm.disconnect()


# --------------------------------------------------------------------------
# Site sign-in gating
# --------------------------------------------------------------------------


async def test_signed_out_members_are_withheld_when_site_signin_is_required(
    door_with_member, device_api_key, set_config
):
    await ws.aget(set_config)(ENABLE_PORTAL_SITE_SIGN_IN=True)
    await ws.aget(door_with_member)(serial="door-signin-01")

    comm, handshake = await ws.open_authenticated(
        "door", "door-signin-01", device_api_key
    )

    assert handshake["by_command"]["sync"]["tags"] == []
    await comm.disconnect()


async def test_signed_in_members_are_synced_when_site_signin_is_required(
    door_with_member, device_api_key, set_config
):
    await ws.aget(set_config)(ENABLE_PORTAL_SITE_SIGN_IN=True)
    _door, profile = await ws.aget(door_with_member)(serial="door-signin-02")

    def _sign_in():
        from api_general.models import SiteSession

        SiteSession.objects.create(user=profile.user)

    await ws.aget(_sign_in)()

    comm, handshake = await ws.open_authenticated(
        "door", "door-signin-02", device_api_key
    )

    assert handshake["by_command"]["sync"]["tags"] == ["TAG-AAA"]
    await comm.disconnect()


async def test_exempt_devices_ignore_the_signin_requirement(
    door_with_member, device_api_key, set_config
):
    await ws.aget(set_config)(ENABLE_PORTAL_SITE_SIGN_IN=True)
    await ws.aget(door_with_member)(serial="door-signin-03", exempt_signin=True)

    comm, handshake = await ws.open_authenticated(
        "door", "door-signin-03", device_api_key
    )

    assert handshake["by_command"]["sync"]["tags"] == ["TAG-AAA"]
    await comm.disconnect()


# --------------------------------------------------------------------------
# Swipe logging
# --------------------------------------------------------------------------


async def test_successful_swipe_is_logged_and_acknowledged(
    door_with_member, device_api_key
):
    from access.models import DoorLog

    door, profile = await ws.aget(door_with_member)(serial="door-log-01")

    comm, _ = await ws.open_authenticated("door", "door-log-01", device_api_key)
    await comm.send_json_to({"command": "log_access", "card_id": "TAG-AAA"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "log_access",
        "success": True,
    }
    await comm.disconnect()

    log = await ws.aget(DoorLog.objects.get)(door=door)
    assert log.success is True
    assert log.user_id == profile.user_id


async def test_successful_swipe_updates_last_seen(door_with_member, device_api_key):
    from profile.models import Profile

    _door, profile = await ws.aget(door_with_member)(serial="door-log-seen")
    assert profile.last_seen is None

    comm, _ = await ws.open_authenticated("door", "door-log-seen", device_api_key)
    await comm.send_json_to({"command": "log_access", "card_id": "TAG-AAA"})
    await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    refreshed = await ws.aget(Profile.objects.get)(pk=profile.pk)
    assert refreshed.last_seen is not None


async def test_denied_swipe_is_logged_as_unsuccessful(door_with_member, device_api_key):
    from access.models import DoorLog

    door, _profile = await ws.aget(door_with_member)(
        serial="door-log-02", member_state="inactive"
    )

    comm, _ = await ws.open_authenticated("door", "door-log-02", device_api_key)
    await comm.send_json_to({"command": "log_access_denied", "card_id": "TAG-AAA"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "log_access_denied",
        "success": True,
    }
    await comm.disconnect()

    log = await ws.aget(DoorLog.objects.get)(door=door)
    assert log.success is False


async def test_denied_swipe_triggers_a_resync(door_with_member, device_api_key):
    """A rejected swipe re-pushes the tag list, on the theory the cache is stale."""
    await ws.aget(door_with_member)(serial="door-log-resync", member_state="inactive")

    comm, _ = await ws.open_authenticated("door", "door-log-resync", device_api_key)
    await comm.send_json_to({"command": "log_access_denied", "card_id": "TAG-AAA"})

    messages = [await comm.receive_json_from(timeout=ws.TIMEOUT) for _ in range(2)]
    assert {m["command"] for m in messages} == {"log_access_denied", "sync"}
    await comm.disconnect()


async def test_lockout_swipe_is_logged_as_unsuccessful(
    door_with_member, device_api_key
):
    from access.models import DoorLog

    door, _profile = await ws.aget(door_with_member)(
        serial="door-log-03", locked_out=True
    )

    comm, _ = await ws.open_authenticated("door", "door-log-03", device_api_key)
    await comm.send_json_to({"command": "log_access_locked_out", "card_id": "TAG-AAA"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "log_access_locked_out",
        "success": True,
    }
    await comm.disconnect()

    log = await ws.aget(DoorLog.objects.get)(door=door)
    assert log.success is False


@pytest.mark.parametrize(
    "command", ["log_access", "log_access_denied", "log_access_locked_out"]
)
async def test_unknown_card_is_acknowledged_without_logging(
    door_with_member, device_api_key, command
):
    """An unrecognised card must not drop the socket.

    Devices are shared, and a visitor tapping a foreign card should not take a
    door offline for everyone else.
    """
    from access.models import DoorLog

    door, _profile = await ws.aget(door_with_member)(serial=f"door-unknown-{command}")

    comm, _ = await ws.open_authenticated(
        "door", f"door-unknown-{command}", device_api_key
    )
    await comm.send_json_to({"command": command, "card_id": "NOT-A-REAL-TAG"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": command,
        "success": True,
    }
    await comm.disconnect()

    assert await ws.aget(DoorLog.objects.filter(door=door).count)() == 0
