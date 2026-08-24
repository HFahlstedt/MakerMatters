"""Characterisation: the interlock protocol — session lifecycle and costing.

Interlocks are the only device type that moves money on its own. A session
accrues cost from three independent rates (per session, per hour, per kWh) and
debits the member's wallet when it ends. Getting this wrong during the upgrade
means silently over- or under-charging every member of the space, so the
arithmetic is pinned here explicitly.
"""

from datetime import timedelta

import pytest

from tests import ws

pytestmark = [pytest.mark.protocol, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def interlock_with_member(make_interlock, make_member):
    def _build(
        serial="interlock-01",
        member_state="active",
        rfid="TAG-INT",
        link=True,
        **interlock_kwargs,
    ):
        interlock = make_interlock(serial=serial, authorised=True, **interlock_kwargs)
        profile = make_member(state=member_state, rfid=rfid)
        if link:
            profile.interlocks.add(interlock)
        return interlock, profile

    return _build


def _backdate(session_id, seconds):
    """Move a session's start time into the past so elapsed-time costing is testable."""

    def _do():
        from django.utils import timezone

        from access.models import InterlockLog

        log = InterlockLog.objects.get(id=session_id)
        log.date_started = timezone.now() - timedelta(seconds=seconds)
        log.save(update_fields=["date_started"])

    return _do


# --------------------------------------------------------------------------
# Starting a session
# --------------------------------------------------------------------------


async def test_authorised_member_starts_a_session(
    interlock_with_member, device_api_key
):
    from access.models import InterlockLog

    interlock, profile = await ws.aget(interlock_with_member)(serial="int-start-01")

    comm, _ = await ws.open_authenticated("interlock", "int-start-01", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )

    reply = await comm.receive_json_from(timeout=ws.TIMEOUT)
    assert reply["command"] == "interlock_session_start"
    assert reply["session_id"]
    await comm.disconnect()

    session = await ws.aget(InterlockLog.objects.get)(id=reply["session_id"])
    assert session.success is True
    assert session.date_ended is None
    assert session.user_started_id == profile.user_id


@pytest.mark.parametrize("member_state", ["inactive", "noob", "accountonly"])
async def test_non_active_members_are_rejected(
    interlock_with_member, device_api_key, member_state
):
    await ws.aget(interlock_with_member)(
        serial=f"int-reject-{member_state}", member_state=member_state
    )

    comm, _ = await ws.open_authenticated(
        "interlock", f"int-reject-{member_state}", device_api_key
    )
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "interlock_session_rejected",
        "reason": "rejected",
    }
    await comm.disconnect()


async def test_members_without_permission_are_rejected(
    interlock_with_member, device_api_key
):
    await ws.aget(interlock_with_member)(serial="int-reject-unlinked", link=False)

    comm, _ = await ws.open_authenticated(
        "interlock", "int-reject-unlinked", device_api_key
    )
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )

    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["reason"] == "rejected"
    await comm.disconnect()


async def test_unknown_card_is_rejected(interlock_with_member, device_api_key):
    await ws.aget(interlock_with_member)(serial="int-reject-unknown")

    comm, _ = await ws.open_authenticated(
        "interlock", "int-reject-unknown", device_api_key
    )
    await comm.send_json_to({"command": "interlock_session_start", "card_id": "NOPE"})

    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["reason"] == "rejected"
    await comm.disconnect()


async def test_maintenance_lockout_is_reported_distinctly(
    interlock_with_member, device_api_key
):
    """Lockout must be distinguishable from a permission failure.

    The firmware shows a different message, and the member gets a different SMS.
    """
    await ws.aget(interlock_with_member)(serial="int-lockout", locked_out=True)

    comm, _ = await ws.open_authenticated("interlock", "int-lockout", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )

    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["reason"] == "locked_out"
    await comm.disconnect()


async def test_signed_out_member_is_rejected_when_signin_required(
    interlock_with_member, device_api_key, set_config
):
    await ws.aget(set_config)(ENABLE_PORTAL_SITE_SIGN_IN=True)
    await ws.aget(interlock_with_member)(serial="int-signin-01")

    comm, _ = await ws.open_authenticated("interlock", "int-signin-01", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )

    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))[
        "reason"
    ] == "not_signed_in"
    await comm.disconnect()


async def test_exempt_interlock_ignores_signin_requirement(
    interlock_with_member, device_api_key, set_config
):
    await ws.aget(set_config)(ENABLE_PORTAL_SITE_SIGN_IN=True)
    await ws.aget(interlock_with_member)(serial="int-signin-02", exempt_signin=True)

    comm, _ = await ws.open_authenticated("interlock", "int-signin-02", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )

    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["command"] == (
        "interlock_session_start"
    )
    await comm.disconnect()


async def test_rejection_records_an_unsuccessful_session(
    interlock_with_member, device_api_key
):
    """Rejections are persisted as closed, zero-cost InterlockLog rows."""
    from access.models import InterlockLog

    interlock, _profile = await ws.aget(interlock_with_member)(
        serial="int-reject-log", member_state="inactive"
    )

    comm, _ = await ws.open_authenticated("interlock", "int-reject-log", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    session = await ws.aget(InterlockLog.objects.get)(interlock=interlock)
    assert session.success is False
    assert session.reason == "rejected"
    assert session.date_ended is not None
    assert session.total_cost == 0


async def test_starting_a_session_closes_any_previous_one(
    interlock_with_member, device_api_key
):
    """Only one session may be open per interlock; a new start force-ends the old."""
    from access.models import InterlockLog

    interlock, _profile = await ws.aget(interlock_with_member)(serial="int-supersede")

    comm, _ = await ws.open_authenticated("interlock", "int-supersede", device_api_key)

    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    first = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    second = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]
    await comm.disconnect()

    assert first != second
    assert (await ws.aget(InterlockLog.objects.get)(id=first)).date_ended is not None
    assert (await ws.aget(InterlockLog.objects.get)(id=second)).date_ended is None


# --------------------------------------------------------------------------
# Updating and ending
# --------------------------------------------------------------------------


async def test_session_update_is_acknowledged_and_accrues_time(
    interlock_with_member, device_api_key
):
    from access.models import InterlockLog

    await ws.aget(interlock_with_member)(serial="int-update")

    comm, _ = await ws.open_authenticated("interlock", "int-update", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await ws.aget(_backdate(session_id, 120))()
    await comm.send_json_to(
        {
            "command": "interlock_session_update",
            "session_id": session_id,
            "session_kwh": 1.5,
        }
    )

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "interlock_session_update",
        "success": True,
    }
    await comm.disconnect()

    session = await ws.aget(InterlockLog.objects.get)(id=session_id)
    assert session.total_time.total_seconds() >= 120
    assert session.total_kwh == 1.5
    assert session.date_ended is None


async def test_short_sessions_are_free(interlock_with_member, device_api_key):
    """Under 10 seconds costs nothing, regardless of the per-session rate.

    This is the guard against a mis-swipe billing someone.
    """
    from access.models import InterlockLog
    from memberbucks.models import MemberBucks

    await ws.aget(interlock_with_member)(serial="int-short", cost_per_session=500)

    comm, _ = await ws.open_authenticated("interlock", "int-short", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await comm.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "TAG-INT",
        }
    )
    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["success"] is True
    await comm.disconnect()

    session = await ws.aget(InterlockLog.objects.get)(id=session_id)
    assert session.total_cost == 0
    assert await ws.aget(MemberBucks.objects.count)() == 0


async def test_session_cost_combines_fixed_hourly_and_energy_rates(
    interlock_with_member, device_api_key
):
    """cost = per_session + hours x per_hour + kWh x per_kwh, in cents.

    Here: 100 + (1h x 600) + (2kWh x 50) = 800 cents, debited as -8.00.
    """
    from access.models import InterlockLog
    from memberbucks.models import MemberBucks

    _interlock, profile = await ws.aget(interlock_with_member)(
        serial="int-cost",
        cost_per_session=100,
        cost_per_hour=600,
        cost_per_kwh=50,
    )

    comm, _ = await ws.open_authenticated("interlock", "int-cost", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await ws.aget(_backdate(session_id, 3600))()
    await comm.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "TAG-INT",
            "session_kwh": 2,
        }
    )
    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["success"] is True
    await comm.disconnect()

    session = await ws.aget(InterlockLog.objects.get)(id=session_id)
    assert session.date_ended is not None
    assert session.total_cost == 800
    assert session.total_kwh == 2

    charge = await ws.aget(MemberBucks.objects.get)(user_id=profile.user_id)
    assert charge.transaction_type == "interlock"
    assert charge.amount == pytest.approx(-8.0)


async def test_energy_cost_uses_the_latest_reading(
    interlock_with_member, device_api_key
):
    """Each update bills the reading supplied with it, not the previous one.

    Sequence: an update reports 2 kWh, then session_end reports 5 kWh. The
    final cost must use 5, giving 100 + 600 + (5 x 50) = 950 cents.
    """
    from access.models import InterlockLog

    await ws.aget(interlock_with_member)(
        serial="int-kwh-latest",
        cost_per_session=100,
        cost_per_hour=600,
        cost_per_kwh=50,
    )

    comm, _ = await ws.open_authenticated("interlock", "int-kwh-latest", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await ws.aget(_backdate(session_id, 3600))()
    await comm.send_json_to(
        {
            "command": "interlock_session_update",
            "session_id": session_id,
            "session_kwh": 2,
        }
    )
    await comm.receive_json_from(timeout=ws.TIMEOUT)

    await ws.aget(_backdate(session_id, 3600))()
    await comm.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "TAG-INT",
            "session_kwh": 5,
        }
    )
    await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    session = await ws.aget(InterlockLog.objects.get)(id=session_id)
    assert session.total_kwh == 5
    assert session.total_cost == 950


async def test_energy_is_billed_when_reported_only_at_session_end(
    interlock_with_member, device_api_key
):
    """The case that used to be billed as zero energy."""
    from access.models import InterlockLog

    await ws.aget(interlock_with_member)(
        serial="int-kwh-end-only", cost_per_session=0, cost_per_hour=0, cost_per_kwh=50
    )

    comm, _ = await ws.open_authenticated(
        "interlock", "int-kwh-end-only", device_api_key
    )
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await ws.aget(_backdate(session_id, 60))()
    await comm.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "TAG-INT",
            "session_kwh": 3,
        }
    )
    await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    session = await ws.aget(InterlockLog.objects.get)(id=session_id)
    assert session.total_cost == 150  # 3 kWh x 50c


async def test_session_charge_can_push_a_member_negative(
    interlock_with_member, device_api_key
):
    """Balance is never checked before starting or ending a session.

    An interlock will happily run and then overdraw the member. The source
    carries a TODO acknowledging this; pinned so the refactor is a deliberate
    behaviour change rather than an accident.
    """
    from profile.models import Profile

    _interlock, profile = await ws.aget(interlock_with_member)(
        serial="int-negative", cost_per_hour=1000
    )
    assert profile.memberbucks_balance == 0.0

    comm, _ = await ws.open_authenticated("interlock", "int-negative", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await ws.aget(_backdate(session_id, 3600))()
    await comm.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "TAG-INT",
        }
    )
    await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    refreshed = await ws.aget(Profile.objects.get)(pk=profile.pk)
    assert refreshed.memberbucks_balance == pytest.approx(-10.0)


async def test_ending_an_already_ended_session_is_refused(
    interlock_with_member, device_api_key
):
    """Guards against double-billing when firmware retries after a dropped reply."""
    await ws.aget(interlock_with_member)(serial="int-double-end", cost_per_session=100)

    comm, _ = await ws.open_authenticated("interlock", "int-double-end", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await comm.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "TAG-INT",
        }
    )
    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["success"] is True

    await comm.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "TAG-INT",
        }
    )
    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "interlock_session_end",
        "success": False,
        "reason": "session_already_ended",
    }
    await comm.disconnect()


async def test_updating_an_ended_session_is_refused(
    interlock_with_member, device_api_key
):
    await ws.aget(interlock_with_member)(serial="int-update-ended")

    comm, _ = await ws.open_authenticated(
        "interlock", "int-update-ended", device_api_key
    )
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]

    await comm.send_json_to(
        {
            "command": "interlock_session_end",
            "session_id": session_id,
            "card_id": "TAG-INT",
        }
    )
    await comm.receive_json_from(timeout=ws.TIMEOUT)

    await comm.send_json_to(
        {"command": "interlock_session_update", "session_id": session_id}
    )
    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "interlock_session_update",
        "success": False,
        "reason": "session_already_ended",
    }
    await comm.disconnect()


async def test_reconnecting_force_ends_sessions_left_open(
    interlock_with_member, device_api_key
):
    """A power-cycled interlock must not leave a session accruing time forever."""
    from access.models import InterlockLog

    await ws.aget(interlock_with_member)(serial="int-reconnect")

    comm, _ = await ws.open_authenticated("interlock", "int-reconnect", device_api_key)
    await comm.send_json_to(
        {"command": "interlock_session_start", "card_id": "TAG-INT"}
    )
    session_id = (await comm.receive_json_from(timeout=ws.TIMEOUT))["session_id"]
    await comm.disconnect()

    assert (await ws.aget(InterlockLog.objects.get)(id=session_id)).date_ended is None

    comm2, _ = await ws.open_authenticated("interlock", "int-reconnect", device_api_key)
    await comm2.disconnect()

    assert (
        await ws.aget(InterlockLog.objects.get)(id=session_id)
    ).date_ended is not None
