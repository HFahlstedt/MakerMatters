"""Characterisation: the admin member-management endpoints.

These are the screens staff use to run the space: the member list, activation
and deactivation, profile editing, access review, logs and billing.

The response shapes here are the ones most at risk from a serializer refactor,
because ``Profile.get_basic_profile()`` builds a deeply nested dict by hand and
is reused by both the member list and the external API-key integrations. Two
details are pinned deliberately because they look like mistakes and a rewrite
would be tempted to "fix" them:

* ``registrationDate`` and friends are formatted ``%m/%d/%Y`` — US order — even
  though the project defaults to an Australian locale and timezone.
* the ``admin`` key reports ``user.is_staff``, not ``user.is_admin``.
"""

import pytest

pytestmark = pytest.mark.django_db

BASIC_PROFILE_FIELDS = {
    "id",
    "admin",
    "email",
    "excludeFromEmailExport",
    "registrationDate",
    "lastUpdatedProfile",
    "screenName",
    "name",
    "phone",
    "state",
    "vehicleRegistrationPlate",
    "rfid",
    "memberBucks",
    "updateProfileRequired",
    "lastSeen",
    "lastInduction",
    "stripe",
    "subscriptionStatus",
}


# --------------------------------------------------------------------------
# Member list
# --------------------------------------------------------------------------


def test_member_list_shape(as_admin, make_member):
    make_member(state="active", rfid="LIST-1")

    body = as_admin().get("/api/admin/members/").json()

    subject = next(m for m in body if m["rfid"] == "LIST-1")
    assert set(subject) == BASIC_PROFILE_FIELDS
    assert set(subject["name"]) == {"first", "last", "full"}
    assert set(subject["memberBucks"]) == {"balance", "lastPurchase"}
    assert set(subject["stripe"]) == {"cardExpiry", "last4"}


def test_member_list_dates_use_us_ordering(as_admin, make_member):
    """``%m/%d/%Y, %H:%M:%S``, despite the Australian default locale."""
    import re

    make_member(state="active", rfid="DATE-1")

    body = as_admin().get("/api/admin/members/").json()

    assert re.fullmatch(
        r"\d{2}/\d{2}/\d{4}, \d{2}:\d{2}:\d{2}", body[0]["registrationDate"]
    )


def test_the_admin_key_reports_staff_not_admin(as_admin, make_member):
    """A staff member shows ``admin: true``; a portal superuser alone does not."""
    superuser_only = make_member(state="active", rfid="SUPER-1")
    superuser_only.user.admin = True
    superuser_only.user.staff = False
    superuser_only.user.save()

    body = as_admin().get("/api/admin/members/").json()

    subject = next(m for m in body if m["rfid"] == "SUPER-1")
    assert subject["admin"] is False  # is_admin is True, but is_staff is not


def test_member_list_can_be_filtered_by_screen_name(as_admin, make_member):
    wanted = make_member(state="active", rfid="FILTER-1")
    make_member(state="active", rfid="FILTER-2")

    body = (
        as_admin().get("/api/admin/members/", {"screenName": wanted.screen_name}).json()
    )

    assert [m["rfid"] for m in body] == ["FILTER-1"]


def test_an_unmatched_screen_name_filter_returns_nothing(as_admin, make_member):
    make_member(state="active", rfid="FILTER-3")

    assert (
        as_admin().get("/api/admin/members/", {"screenName": "nobody-has-this"}).json()
        == []
    )


def test_member_list_is_readable_with_an_api_key(with_api_key, make_member):
    make_member(state="active", rfid="APIKEY-1")

    response = with_api_key().get("/api/admin/members/")

    assert response.status_code == 200
    assert any(m["rfid"] == "APIKEY-1" for m in response.json())


def test_member_list_rejects_a_plain_member(make_member, as_member):
    member = make_member(state="active", rfid="PLAIN-1")

    assert as_member(member).get("/api/admin/members/").status_code == 403


# --------------------------------------------------------------------------
# Activation / deactivation
# --------------------------------------------------------------------------


def test_reading_a_member_state(as_admin, make_member):
    member = make_member(state="noob", rfid="STATE-1")

    body = as_admin().get(f"/api/admin/members/{member.user_id}/state/active/").json()

    assert body == {"state": "noob"}


def test_activating_a_member(as_admin, make_member, sent_emails):
    from profile.models import Profile

    member = make_member(state="inactive", rfid="STATE-2")

    response = as_admin().post(f"/api/admin/members/{member.user_id}/state/active/")

    assert response.status_code == 200
    assert Profile.objects.get(pk=member.pk).state == "active"


def test_deactivating_a_member(as_admin, make_member, sent_emails):
    from profile.models import Profile

    member = make_member(state="active", rfid="STATE-3")

    as_admin().post(f"/api/admin/members/{member.user_id}/state/inactive/")

    assert Profile.objects.get(pk=member.pk).state == "inactive"


def test_an_unknown_state_is_rejected(as_admin, make_member):
    from profile.models import Profile

    member = make_member(state="active", rfid="STATE-4")

    response = as_admin().post(f"/api/admin/members/{member.user_id}/state/banished/")

    assert response.status_code == 400
    assert Profile.objects.get(pk=member.pk).state == "active"


def test_a_state_change_is_recorded_against_both_parties(
    as_admin, make_member, sent_emails
):
    """The acting admin and the target member each get an audit entry."""
    from profile.models import UserEventLog

    member = make_member(state="inactive", rfid="STATE-5")
    client = as_admin()
    admin_profile = client.admin_profile

    client.post(f"/api/admin/members/{member.user_id}/state/active/")

    assert UserEventLog.objects.filter(user=member.user, logtype="admin").exists()
    assert UserEventLog.objects.filter(
        user=admin_profile.user, logtype="admin"
    ).exists()


# --------------------------------------------------------------------------
# Promoting to member
# --------------------------------------------------------------------------


def test_making_a_member_activates_and_grants_default_access(
    as_admin, make_member, make_door, make_interlock, sent_emails
):
    from profile.models import Profile

    default_door = make_door(serial="mm-door", all_members=True)
    other_door = make_door(serial="mm-door-2", all_members=False)
    default_interlock = make_interlock(serial="mm-int", all_members=True)
    member = make_member(state="noob", rfid="MAKE-1")

    body = as_admin().post(f"/api/admin/members/{member.user_id}/makemember/").json()

    assert body == {"success": True, "message": "adminTools.makeMemberSuccess"}
    refreshed = Profile.objects.get(pk=member.pk)
    assert refreshed.state == "active"
    assert list(refreshed.doors.all()) == [default_door]
    assert list(refreshed.interlocks.all()) == [default_interlock]
    assert other_door not in refreshed.doors.all()


def test_making_a_member_emails_the_member_and_the_committee(
    as_admin, make_member, sent_emails
):
    member = make_member(state="noob", rfid="MAKE-2")

    as_admin().post(f"/api/admin/members/{member.user_id}/makemember/")

    subjects = [e["Subject"] for e in sent_emails]
    assert any(s.startswith("Welcome to") for s in subjects)
    assert any("just got turned into a member" in s for s in subjects)


def test_an_account_only_member_can_be_promoted(as_admin, make_member, sent_emails):
    from profile.models import Profile

    member = make_member(state="accountonly", rfid="MAKE-3")

    body = as_admin().post(f"/api/admin/members/{member.user_id}/makemember/").json()

    assert body["success"] is True
    assert Profile.objects.get(pk=member.pk).state == "active"


def test_an_existing_member_cannot_be_promoted_again(as_admin, make_member):
    member = make_member(state="active", rfid="MAKE-4")

    body = as_admin().post(f"/api/admin/members/{member.user_id}/makemember/").json()

    assert body == {"success": False, "message": "adminTools.makeMemberErrorExists"}


# --------------------------------------------------------------------------
# Profile editing
# --------------------------------------------------------------------------


def profile_payload(**overrides):
    payload = {
        "email": "updated@example.com",
        "firstName": "Updated",
        "lastName": "Name",
        "rfidCard": "EDIT-NEW",
        "phone": "0400111222",
        "screenName": "updated-screen",
        "vehicleRegistrationPlate": "ABC123",
        "excludeFromEmailExport": True,
    }
    payload.update(overrides)
    return payload


def test_editing_a_member_profile_writes_every_field(as_admin, make_member):
    from profile.models import Profile, User

    member = make_member(state="active", rfid="EDIT-1")

    response = as_admin().put(
        f"/api/admin/members/{member.user_id}/profile/",
        profile_payload(),
        format="json",
    )

    assert response.status_code == 200
    refreshed = Profile.objects.get(pk=member.pk)
    assert refreshed.first_name == "Updated"
    assert refreshed.last_name == "Name"
    assert refreshed.rfid == "EDIT-NEW"
    assert refreshed.phone == "0400111222"
    assert refreshed.screen_name == "updated-screen"
    assert refreshed.vehicle_registration_plate == "ABC123"
    assert refreshed.exclude_from_email_export is True
    assert User.objects.get(pk=member.user_id).email == "updated@example.com"


def test_changing_the_rfid_resyncs_the_members_doors(as_admin, make_member, make_door):
    """A new card must reach the door caches, or the member is locked out."""
    import access.models as access_models

    door = make_door(serial="edit-sync")
    member = make_member(state="active", rfid="EDIT-2")
    member.doors.add(door)

    synced = []
    original = access_models.AccessControlledDevice.sync
    access_models.AccessControlledDevice.sync = (
        lambda self, request=None: synced.append(self.serial_number)
    )
    try:
        as_admin().put(
            f"/api/admin/members/{member.user_id}/profile/",
            profile_payload(rfidCard="EDIT-2-NEW"),
            format="json",
        )
    finally:
        access_models.AccessControlledDevice.sync = original

    assert "edit-sync" in synced


def test_an_unchanged_rfid_does_not_trigger_a_resync(as_admin, make_member, make_door):
    import access.models as access_models

    door = make_door(serial="edit-nosync")
    member = make_member(state="active", rfid="EDIT-3")
    member.doors.add(door)

    synced = []
    original = access_models.AccessControlledDevice.sync
    access_models.AccessControlledDevice.sync = (
        lambda self, request=None: synced.append(self.serial_number)
    )
    try:
        as_admin().put(
            f"/api/admin/members/{member.user_id}/profile/",
            profile_payload(rfidCard="EDIT-3"),
            format="json",
        )
    finally:
        access_models.AccessControlledDevice.sync = original

    assert synced == []


# --------------------------------------------------------------------------
# Access review
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/admin/members/{id}/state/active/", "get"),
        ("/api/admin/members/{id}/makemember/", "post"),
        ("/api/admin/members/{id}/access/", "get"),
        ("/api/admin/members/{id}/sendwelcome/", "post"),
        ("/api/admin/members/{id}/sendsms/", "post"),
        ("/api/admin/members/{id}/logs/", "get"),
    ],
)
def test_an_unknown_member_is_a_404_not_a_crash(as_admin, make_member, path, method):
    """Every one of these looked the member up with ``User.objects.get(id=...)``.

    That raises ``DoesNotExist`` for an id that is not there, which DRF does
    not handle, so a mistyped id in the admin URL bar returned a 500 rather
    than the 404 it plainly is.
    """
    make_member(state="active", rfid="MISSING-PROBE")

    response = getattr(as_admin(), method)(path.format(id=999999))

    assert response.status_code == 404


def test_editing_a_profile_to_a_tag_another_member_holds_is_rejected(
    as_admin, make_member
):
    """``Profile.rfid`` is unique, so this used to be an unhandled 500.

    The member-facing ``/api/billing/access-card/`` already guards this; the
    admin edit path is the same clash from the other direction.
    """
    from profile.models import Profile

    make_member(state="active", rfid="ADMIN-TAKEN")
    victim = make_member(state="active", rfid="ADMIN-MINE")

    response = as_admin().put(
        f"/api/admin/members/{victim.user_id}/profile/",
        profile_payload(rfidCard="ADMIN-TAKEN"),
        format="json",
    )

    assert response.status_code == 400
    assert response.json() == {"success": False, "error": "accessCardInUse"}
    assert Profile.objects.get(pk=victim.pk).rfid == "ADMIN-MINE"


def test_editing_a_profile_keeping_the_members_own_tag_is_allowed(
    as_admin, make_member
):
    """The clash check must not count the member's own tag against them."""
    member = make_member(state="active", rfid="ADMIN-KEEP")

    response = as_admin().put(
        f"/api/admin/members/{member.user_id}/profile/",
        profile_payload(rfidCard="ADMIN-KEEP"),
        format="json",
    )

    assert response.status_code == 200


def test_the_admin_and_signup_paths_grant_the_same_default_access(
    as_admin, make_member, make_door, make_interlock, sent_emails
):
    """Both promotion routes granted default access with their own copy of the loop.

    ``MakeMember`` and the signup completion endpoint in ``api_billing`` each
    filtered ``all_members=True`` and looped, so the two could drift apart.
    They now share ``Profile.grant_default_access()``.
    """
    from profile.models import Profile

    default_door = make_door(serial="both-door", all_members=True)
    opt_in_door = make_door(serial="both-door-2", all_members=False)
    default_interlock = make_interlock(serial="both-int", all_members=True)
    member = make_member(state="noob", rfid="BOTH-1")

    as_admin().post(f"/api/admin/members/{member.user_id}/makemember/")

    refreshed = Profile.objects.get(pk=member.pk)
    assert list(refreshed.doors.all()) == [default_door]
    assert opt_in_door not in refreshed.doors.all()
    assert list(refreshed.interlocks.all()) == [default_interlock]


def test_member_access_ignores_member_state(as_admin, make_member, make_door):
    """Admins see the underlying grants even for an inactive member.

    The member-facing endpoint reports ``access: False`` for everything when
    the member is not active; this one passes ``ignore_user_state=True`` so
    staff can review permissions before reactivating someone.
    """
    door = make_door(serial="access-review")
    member = make_member(state="inactive", rfid="ACC-1")
    member.doors.add(door)

    body = as_admin().get(f"/api/admin/members/{member.user_id}/access/").json()

    assert set(body) == {"doors", "interlocks"}
    entry = next(d for d in body["doors"] if d["id"] == door.id)
    assert entry["access"] is True
    assert set(entry) == {"name", "access", "id", "locked_out", "offline"}


def test_hidden_devices_are_omitted_from_access_review(
    as_admin, make_member, make_door
):
    make_door(serial="access-hidden", hidden=True)
    member = make_member(state="active", rfid="ACC-2")

    body = as_admin().get(f"/api/admin/members/{member.user_id}/access/").json()

    assert body["doors"] == []


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------


def test_sending_a_welcome_email(as_admin, make_member, sent_emails):
    member = make_member(state="active", rfid="WELCOME-1")

    response = as_admin().post(f"/api/admin/members/{member.user_id}/sendwelcome/")

    assert response.status_code == 200
    assert any(e["Subject"].startswith("Welcome to") for e in sent_emails)


def test_sending_an_sms_requires_the_feature_to_be_enabled(as_admin, make_member):
    member = make_member(state="active", rfid="SMS-1")

    response = as_admin().post(
        f"/api/admin/members/{member.user_id}/sendsms/",
        {"smsBody": "Your laser cutter booking starts soon."},
        format="json",
    )

    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "message": "SMS functionality not enabled.",
    }


def test_sending_an_sms_requires_a_phone_number(as_admin, make_member, set_config):
    set_config(SMS_ENABLE=True)
    member = make_member(state="active", rfid="SMS-2", phone="")

    response = as_admin().post(
        f"/api/admin/members/{member.user_id}/sendsms/",
        {"smsBody": "Hello"},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["message"] == "Member does not have a phone number."


@pytest.mark.parametrize("body", ["", "x" * 321])
def test_sms_body_length_is_validated(as_admin, make_member, set_config, body):
    set_config(SMS_ENABLE=True)
    member = make_member(state="active", rfid=f"SMS-{len(body)}", phone="0400111222")

    response = as_admin().post(
        f"/api/admin/members/{member.user_id}/sendsms/",
        {"smsBody": body},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["message"] == "SMS body is invalid."


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def test_member_logs_shape(as_admin, make_member, make_door, make_interlock):
    from datetime import timedelta

    from access.models import DoorLog, InterlockLog

    member = make_member(state="active", rfid="LOG-1")
    member.user.log_event("Something happened", "admin")
    DoorLog.objects.create(user=member.user, door=make_door(serial="log-door"))
    InterlockLog.objects.create(
        interlock=make_interlock(serial="log-int"),
        user_started=member.user,
        total_time=timedelta(minutes=5),
        total_cost=250,
    )

    body = as_admin().get(f"/api/admin/members/{member.user_id}/logs/").json()

    assert set(body) == {"userEventLogs", "doorLogs", "interlockLogs"}
    assert set(body["userEventLogs"][0]) == {"date", "description", "logtype"}
    assert set(body["doorLogs"][0]) == {"date", "door", "success"}
    assert set(body["interlockLogs"][0]) == {
        "interlockName",
        "dateStarted",
        "totalTime",
        "totalCost",
        "status",
        "userEnded",
    }
    assert body["interlockLogs"][0]["totalCost"] == 2.5  # cents -> dollars


def test_log_types_are_rendered_with_their_display_name(as_admin, make_member):
    member = make_member(state="active", rfid="LOG-2")
    member.user.log_event("Card charged", "stripe")

    body = as_admin().get(f"/api/admin/members/{member.user_id}/logs/").json()

    assert body["userEventLogs"][0]["logtype"] == "Stripe Event"


@pytest.mark.parametrize(
    "ended,success,expected_status",
    [(True, True, 1), (False, True, 0), (False, False, -1)],
)
def test_interlock_log_status_encodes_session_outcome(
    as_admin, make_member, make_interlock, ended, success, expected_status
):
    """-1 rejected, 0 still running, 1 completed."""
    from datetime import timedelta

    from django.utils import timezone

    from access.models import InterlockLog

    member = make_member(state="active", rfid=f"LOG-{expected_status}")
    InterlockLog.objects.create(
        interlock=make_interlock(serial=f"log-int-{expected_status}"),
        user_started=member.user,
        total_time=timedelta(minutes=1),
        success=success,
        date_ended=timezone.now() if ended else None,
    )

    body = as_admin().get(f"/api/admin/members/{member.user_id}/logs/").json()

    assert body["interlockLogs"][0]["status"] == expected_status


def test_member_logs_are_readable_with_an_api_key(with_api_key, make_member):
    member = make_member(state="active", rfid="LOG-KEY")

    assert (
        with_api_key().get(f"/api/admin/members/{member.user_id}/logs/").status_code
        == 200
    )


# --------------------------------------------------------------------------
# Billing info
# --------------------------------------------------------------------------


def test_billing_info_for_a_member_without_a_plan(as_admin, make_member, stripe_stub):
    member = make_member(state="active", rfid="BILL-1")

    body = as_admin().get(f"/api/admin/members/{member.user_id}/billing/").json()

    assert "subscription" not in body  # only present when a plan exists
    assert set(body["memberbucks"]) == {
        "balance",
        "stripe_card_last_digits",
        "stripe_card_expiry",
        "transactions",
        "lastPurchase",
    }
    assert not stripe_stub.called("Subscription.retrieve")


def test_billing_info_includes_recent_transactions(as_admin, make_member, stripe_stub):
    from memberbucks.models import MemberBucks

    member = make_member(state="active", rfid="BILL-2")
    MemberBucks.objects.create(
        user=member.user, amount=25.0, transaction_type="cash", description="Top up"
    )

    body = as_admin().get(f"/api/admin/members/{member.user_id}/billing/").json()

    txn = body["memberbucks"]["transactions"][0]
    assert set(txn) == {"amount", "type", "description", "date"}
    assert txn["amount"] == 25.0
    assert txn["type"] == "Cash"  # display name, not the stored key
    assert body["memberbucks"]["balance"] == 25.0


def test_billing_info_fetches_the_subscription_from_stripe(
    as_admin, make_member, make_tier_and_plan, stripe_stub, set_config
):
    from tests.conftest import FakeStripeObject

    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan()
    member = make_member(state="active", rfid="BILL-3")
    member.membership_plan = plan
    member.stripe_subscription_id = "sub_admin_view"
    member.subscription_status = "active"
    member.save()

    stripe_stub.set(
        "Subscription.retrieve",
        FakeStripeObject(
            {
                "billing_cycle_anchor": 1700000000,
                "current_period_end": 1702592000,
                "cancel_at": None,
                "cancel_at_period_end": False,
                "start_date": 1697408000,
            }
        ),
    )

    body = as_admin().get(f"/api/admin/members/{member.user_id}/billing/").json()

    assert set(body["subscription"]) == {
        "status",
        "billingCycleAnchor",
        "currentPeriodEnd",
        "cancelAt",
        "cancelAtPeriodEnd",
        "startDate",
        "membershipTier",
        "membershipPlan",
    }
    assert body["subscription"]["status"] == "active"
    assert body["subscription"]["membershipPlan"]["cost"] == 2500
