"""Characterisation: authentication, registration and site sessions.

``api_general`` is 793 lines and fifteen endpoints covering everything a member
does before they reach the rest of the system: logging in (by password, by RFID
at a kiosk, or via Discourse SSO), registering, verifying an email address,
resetting a password, and signing in and out of the physical space.

None of it was covered. That matters more than the line count suggests, because
``SiteSignIn``/``SiteSignOut`` drive ``Profile.is_signed_into_site()``, which
``AccessControlledDevice.get_tags()`` consults when deciding whose card opens a
door. The access-control tests pin what ``get_tags`` does with that answer;
these pin how the answer is produced.

Passwords here are fixtures, not secrets — ``make_member`` sets a known one.
"""

import base64
import datetime
import hashlib
import hmac
from urllib.parse import parse_qs, urlencode

import pytest

pytestmark = pytest.mark.django_db

PASSWORD = "test-password-not-a-secret"
SSO_SECRET = "sso-shared-secret-for-tests"
RETURN_URL = "https://forum.example.com/session/sso_login"


def sso_request(secret=SSO_SECRET, nonce="nonce-123", return_url=RETURN_URL):
    """Build the payload Discourse sends when it delegates a login to us."""
    payload = base64.b64encode(
        urlencode({"nonce": nonce, "return_sso_url": return_url}).encode("utf-8")
    )
    signature = hmac.new(
        secret.encode("utf-8"), payload, digestmod=hashlib.sha256
    ).hexdigest()

    return {"sso": payload.decode("utf-8"), "sig": signature}


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


def test_logging_in_with_the_right_password(api_client, make_member):
    member = make_member(state="active", rfid="LOGIN-1")

    response = api_client.post(
        "/api/login/", {"email": member.user.email, "password": PASSWORD}, format="json"
    )

    assert response.status_code == 200
    assert api_client.get("/api/loggedin/").status_code == 200


def test_logging_in_with_the_wrong_password(api_client, make_member):
    member = make_member(state="active", rfid="LOGIN-2")

    response = api_client.post(
        "/api/login/",
        {"email": member.user.email, "password": "not-the-password"},
        format="json",
    )

    assert response.status_code == 401
    assert api_client.get("/api/loggedin/").status_code == 401


def test_logging_in_is_case_insensitive_on_the_email(api_client, make_member):
    """``get_by_natural_key`` matches ``email__iexact``."""
    member = make_member(state="active", rfid="LOGIN-3")

    response = api_client.post(
        "/api/login/",
        {"email": member.user.email.upper(), "password": PASSWORD},
        format="json",
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "body", [{}, {"email": "someone@example.com"}, {"password": PASSWORD}]
)
def test_logging_in_without_both_fields_is_a_400(api_client, body):
    assert api_client.post("/api/login/", body, format="json").status_code == 400


def test_an_unverified_member_cannot_log_in_and_is_re_sent_the_link(
    api_client, make_member, sent_emails
):
    from api_general.models import EmailVerificationToken

    member = make_member(state="active", rfid="LOGIN-4")
    member.user.email_verified = False
    member.user.save()

    response = api_client.post(
        "/api/login/", {"email": member.user.email, "password": PASSWORD}, format="json"
    )

    assert response.status_code == 403
    assert response.json() == {"message": "loginCard.emailNotVerified"}
    assert EmailVerificationToken.objects.filter(user=member.user).count() == 1
    assert len(sent_emails) == 1


def test_every_failed_unverified_login_mints_another_token(
    api_client, make_member, sent_emails
):
    """DEFECT, pinned: nothing bounds how many tokens one account accumulates.

    Each attempt creates a fresh ``EmailVerificationToken`` and never expires
    or replaces the previous one, so every token ever minted stays valid for
    its 24 hours. Repeated attempts — a member with a saved wrong password, or
    anyone who knows the address — grow the table without limit.
    """
    from api_general.models import EmailVerificationToken

    member = make_member(state="active", rfid="LOGIN-5")
    member.user.email_verified = False
    member.user.save()

    for _ in range(3):
        api_client.post(
            "/api/login/",
            {"email": member.user.email, "password": PASSWORD},
            format="json",
        )

    assert EmailVerificationToken.objects.filter(user=member.user).count() == 3


def test_logging_in_while_already_logged_in_is_a_no_op(api_client, make_member):
    member = make_member(state="active", rfid="LOGIN-6")
    api_client.force_authenticate(user=member.user)

    assert api_client.post("/api/login/", {}, format="json").status_code == 200


# --------------------------------------------------------------------------
# Discourse SSO
# --------------------------------------------------------------------------


def test_discourse_sso_returns_a_signed_redirect(api_client, make_member, set_config):
    """We are the identity provider: Discourse sends a nonce, we sign it back."""
    set_config(
        ENABLE_DISCOURSE_SSO_PROTOCOL=True,
        DISCOURSE_SSO_PROTOCOL_SECRET_KEY=SSO_SECRET,
    )
    member = make_member(state="active", rfid="SSO-1")

    response = api_client.post(
        "/api/login/",
        {"email": member.user.email, "password": PASSWORD, "sso": sso_request()},
        format="json",
    )

    assert response.status_code == 200
    redirect = response.json()["redirect"]
    assert redirect.startswith(f"{RETURN_URL}?sso=")

    query = parse_qs(redirect.split("?", 1)[1])
    returned = parse_qs(base64.b64decode(query["sso"][0]).decode("utf-8"))
    assert returned["nonce"] == ["nonce-123"]
    assert returned["email"] == [member.user.email]
    assert returned["external_id"] == [str(member.user.id)]
    assert returned["username"] == [member.screen_name]

    expected_sig = hmac.new(
        SSO_SECRET.encode("utf-8"),
        query["sso"][0].encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    assert query["sig"] == [expected_sig]


def test_discourse_sso_rejects_a_bad_signature(api_client, make_member, set_config):
    set_config(
        ENABLE_DISCOURSE_SSO_PROTOCOL=True,
        DISCOURSE_SSO_PROTOCOL_SECRET_KEY=SSO_SECRET,
    )
    member = make_member(state="active", rfid="SSO-2")
    forged = sso_request(secret="the-wrong-secret")

    response = api_client.post(
        "/api/login/",
        {"email": member.user.email, "password": PASSWORD, "sso": forged},
        format="json",
    )

    assert response.status_code == 400


def test_discourse_sso_is_refused_when_the_feature_is_off(
    api_client, make_member, set_config
):
    set_config(ENABLE_DISCOURSE_SSO_PROTOCOL=False)
    member = make_member(state="active", rfid="SSO-3")

    response = api_client.post(
        "/api/login/",
        {"email": member.user.email, "password": PASSWORD, "sso": sso_request()},
        format="json",
    )

    assert response.status_code == 400


def test_an_already_logged_in_member_still_gets_an_sso_redirect(
    api_client, make_member, set_config
):
    """The session is reused rather than forcing a second password entry."""
    set_config(
        ENABLE_DISCOURSE_SSO_PROTOCOL=True,
        DISCOURSE_SSO_PROTOCOL_SECRET_KEY=SSO_SECRET,
    )
    member = make_member(state="active", rfid="SSO-4")
    api_client.force_authenticate(user=member.user)

    response = api_client.post("/api/login/", {"sso": sso_request()}, format="json")

    assert response.status_code == 200
    assert response.json()["redirect"].startswith(RETURN_URL)


# --------------------------------------------------------------------------
# Kiosk login
# --------------------------------------------------------------------------


@pytest.fixture
def make_kiosk(db):
    from api_general.models import Kiosk

    def _make(kiosk_id="kiosk-1", authorised=True, **kwargs):
        return Kiosk.objects.create(
            name=kwargs.pop("name", f"Kiosk {kiosk_id}"),
            kiosk_id=kiosk_id,
            authorised=authorised,
            **kwargs,
        )

    return _make


def test_swiping_in_at_a_kiosk_logs_the_member_in(api_client, make_member, make_kiosk):
    make_kiosk(kiosk_id="kiosk-ok")
    make_member(state="active", rfid="KIOSK-TAG")

    response = api_client.post(
        "/api/login/kiosk/",
        {"cardId": "KIOSK-TAG", "kioskId": "kiosk-ok"},
        format="json",
    )

    assert response.status_code == 200
    assert api_client.get("/api/loggedin/").status_code == 200


def test_an_unknown_kiosk_is_rejected(api_client, make_member):
    make_member(state="active", rfid="KIOSK-TAG-2")

    response = api_client.post(
        "/api/login/kiosk/",
        {"cardId": "KIOSK-TAG-2", "kioskId": "no-such-kiosk"},
        format="json",
    )

    assert response.status_code == 401


def test_an_unauthorised_kiosk_is_rejected(api_client, make_member, make_kiosk):
    make_kiosk(kiosk_id="kiosk-off", authorised=False)
    make_member(state="active", rfid="KIOSK-TAG-3")

    response = api_client.post(
        "/api/login/kiosk/",
        {"cardId": "KIOSK-TAG-3", "kioskId": "kiosk-off"},
        format="json",
    )

    assert response.status_code == 403


def test_an_unknown_card_is_rejected(api_client, make_kiosk):
    make_kiosk(kiosk_id="kiosk-unknown-card")

    response = api_client.post(
        "/api/login/kiosk/",
        {"cardId": "NOT-A-TAG", "kioskId": "kiosk-unknown-card"},
        format="json",
    )

    assert response.status_code == 401


def test_a_kiosk_swipe_by_an_unverified_member_is_rejected(
    api_client, make_member, make_kiosk
):
    make_kiosk(kiosk_id="kiosk-unverified")
    member = make_member(state="active", rfid="KIOSK-TAG-4")
    member.user.email_verified = False
    member.user.save()

    response = api_client.post(
        "/api/login/kiosk/",
        {"cardId": "KIOSK-TAG-4", "kioskId": "kiosk-unverified"},
        format="json",
    )

    assert response.status_code == 403
    assert response.json() == {"message": "error.emailNotVerified"}


def test_a_kiosk_swipe_ignores_member_state(api_client, make_member, make_kiosk):
    """DEFECT, pinned: an inactive member can still log in at a kiosk.

    Unlike ``get_tags()``, which filters on ``state="active"``, the kiosk login
    path checks only that the tag matches a profile and the email is verified.
    A member who has been deactivated cannot open a door but can still sign
    into the portal at the kiosk in front of it.
    """
    make_kiosk(kiosk_id="kiosk-inactive")
    make_member(state="inactive", rfid="KIOSK-TAG-5")

    response = api_client.post(
        "/api/login/kiosk/",
        {"cardId": "KIOSK-TAG-5", "kioskId": "kiosk-inactive"},
        format="json",
    )

    assert response.status_code == 200


@pytest.mark.parametrize("body", [{}, {"cardId": "X"}, {"kioskId": "Y"}])
def test_a_kiosk_swipe_without_both_fields_is_a_400(api_client, body):
    assert api_client.post("/api/login/kiosk/", body, format="json").status_code == 400


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------


def test_logging_out_ends_the_session(api_client, make_member):
    member = make_member(state="active", rfid="LOGOUT-1")
    api_client.post(
        "/api/login/", {"email": member.user.email, "password": PASSWORD}, format="json"
    )

    assert api_client.post("/api/logout/").json() == {"success": True}
    assert api_client.get("/api/loggedin/").status_code == 401


# --------------------------------------------------------------------------
# Registration and email verification
# --------------------------------------------------------------------------


def registration_payload(**overrides):
    payload = {
        "email": "newcomer@example.com",
        "password": PASSWORD,
        "firstName": "New",
        "lastName": "Comer",
        "screenName": "newcomer",
        "mobile": "0400000000",
        "vehicleRegistrationPlate": "XYZ789",
    }
    payload.update(overrides)
    return payload


def test_registering_creates_an_unverified_user_and_profile(api_client, sent_emails):
    from api_general.models import EmailVerificationToken
    from profile.models import Profile, User

    response = api_client.post("/api/register/", registration_payload(), format="json")

    assert response.status_code == 200
    user = User.objects.get(email="newcomer@example.com")
    assert user.email_verified is False
    assert user.check_password(PASSWORD)

    profile = Profile.objects.get(user=user)
    assert profile.first_name == "New"
    assert profile.screen_name == "newcomer"
    assert profile.state == "noob"
    assert EmailVerificationToken.objects.filter(user=user).count() == 1


def test_registering_lowercases_the_email(api_client, sent_emails):
    from profile.models import User

    api_client.post(
        "/api/register/",
        registration_payload(email="MixedCase@Example.com"),
        format="json",
    )

    assert User.objects.filter(email="mixedcase@example.com").exists()


def test_registering_a_duplicate_email_is_a_409(api_client, make_member, sent_emails):
    member = make_member(state="active", rfid="REG-1")

    response = api_client.post(
        "/api/register/",
        registration_payload(email=member.user.email),
        format="json",
    )

    assert response.status_code == 409
    assert response.json() == {"message": "error.accountAlreadyExists"}


def test_registering_a_duplicate_screen_name_is_a_409(
    api_client, make_member, sent_emails
):
    member = make_member(state="active", rfid="REG-2")

    response = api_client.post(
        "/api/register/",
        registration_payload(screenName=member.screen_name),
        format="json",
    )

    assert response.status_code == 409
    assert response.json() == {"message": "error.screenNameAlreadyExists"}


def test_registering_without_an_email_crashes(api_client):
    """DEFECT, pinned: the field is read before it is checked.

    ``body.get("email").lower()`` is the first statement in the handler, so a
    payload without an email raises ``AttributeError`` on ``None`` and returns
    a 500 rather than a 400. The same applies to ``screenName``.
    """
    with pytest.raises(AttributeError):
        api_client.post(
            "/api/register/", registration_payload(email=None), format="json"
        )


def test_verifying_an_email_activates_the_account_and_logs_them_in(
    api_client, make_member
):
    from api_general.models import EmailVerificationToken
    from profile.models import User

    member = make_member(state="noob", rfid="VERIFY-1")
    member.user.email_verified = False
    member.user.save()
    token = EmailVerificationToken.objects.create(user=member.user)

    response = api_client.post(f"/api/email/{token.verification_token}/verify/")

    assert response.status_code == 200
    assert User.objects.get(pk=member.user_id).email_verified is True
    # The token is single use.
    assert not EmailVerificationToken.objects.filter(pk=token.pk).exists()
    assert api_client.get("/api/loggedin/").status_code == 200


def test_an_unknown_verification_token_is_a_401(api_client):
    import uuid

    response = api_client.post(f"/api/email/{uuid.uuid4()}/verify/")

    assert response.status_code == 401
    assert response.json() == {"message": "error.emailVerificationFailed"}


def test_an_expired_verification_token_is_replaced(
    api_client, make_member, sent_emails
):
    """Older than 24 hours: the token is swapped for a fresh one and re-sent."""
    from django.utils import timezone

    from api_general.models import EmailVerificationToken
    from profile.models import User

    member = make_member(state="noob", rfid="VERIFY-2")
    member.user.email_verified = False
    member.user.save()
    token = EmailVerificationToken.objects.create(user=member.user)
    EmailVerificationToken.objects.filter(pk=token.pk).update(
        creation_date=timezone.now() - datetime.timedelta(hours=25)
    )

    response = api_client.post(f"/api/email/{token.verification_token}/verify/")

    assert response.status_code == 403
    assert response.json() == {"message": "error.emailVerificationExpired"}
    assert User.objects.get(pk=member.user_id).email_verified is False

    remaining = EmailVerificationToken.objects.filter(user=member.user)
    assert remaining.count() == 1
    assert remaining.first().pk != token.pk
    assert len(sent_emails) == 1


# --------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------


def test_requesting_a_reset_mints_a_key_and_emails_it(
    api_client, make_member, sent_emails
):
    from profile.models import User

    member = make_member(state="active", rfid="RESET-1")

    response = api_client.post(
        "/api/password/reset/", {"email": member.user.email}, format="json"
    )

    assert response.json() == {"success": True}
    assert User.objects.get(pk=member.user_id).password_reset_key is not None
    assert len(sent_emails) == 1


def test_requesting_a_reset_for_an_unknown_email_reports_failure(api_client):
    """DEFECT, pinned: the response distinguishes registered from unregistered.

    A bare ``except`` returns ``{"success": False}`` when the lookup fails, so
    the endpoint doubles as an oracle for whether an address has an account.
    The usual practice is to answer identically either way.
    """
    response = api_client.post(
        "/api/password/reset/", {"email": "nobody@example.com"}, format="json"
    )

    assert response.json() == {"success": False}


def test_a_valid_reset_token_validates(api_client, make_member, sent_emails):
    member = make_member(state="active", rfid="RESET-2")
    member.user.reset_password()

    response = api_client.post(
        "/api/password/reset/",
        {"token": str(member.user.password_reset_key)},
        format="json",
    )

    assert response.json() == {"success": True}


def test_an_expired_reset_token_is_cleared(api_client, make_member, sent_emails):
    from django.utils import timezone

    from profile.models import User

    member = make_member(state="active", rfid="RESET-3")
    member.user.reset_password()
    User.objects.filter(pk=member.user_id).update(
        password_reset_expire=timezone.now() - datetime.timedelta(hours=1)
    )

    response = api_client.post(
        "/api/password/reset/",
        {"token": str(member.user.password_reset_key)},
        format="json",
    )

    assert response.json() == {"success": False}
    refreshed = User.objects.get(pk=member.user_id)
    assert refreshed.password_reset_key is None
    assert refreshed.password_reset_expire is None


def test_a_token_and_password_resets_the_password(api_client, make_member, sent_emails):
    from profile.models import User

    member = make_member(state="active", rfid="RESET-4")
    member.user.reset_password()

    response = api_client.post(
        "/api/password/reset/",
        {"token": str(member.user.password_reset_key), "password": "a-brand-new-one"},
        format="json",
    )

    assert response.json() == {"success": True}
    refreshed = User.objects.get(pk=member.user_id)
    assert refreshed.check_password("a-brand-new-one")
    assert refreshed.password_reset_key is None


def test_resetting_with_an_unknown_token_crashes(api_client):
    """DEFECT, pinned: only the validate branch catches a missing user.

    The validate-only branch wraps its lookup in ``try/except DoesNotExist``;
    the branch that actually changes the password does not, so an unknown
    token there is a 500 instead of the ``{"success": False}`` its sibling
    returns.
    """
    import uuid

    from profile.models import User

    with pytest.raises(User.DoesNotExist):
        api_client.post(
            "/api/password/reset/",
            {"token": str(uuid.uuid4()), "password": "irrelevant"},
            format="json",
        )


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

PROFILE_FIELDS = {
    "id",
    "email",
    "fullName",
    "firstName",
    "lastName",
    "screenName",
    "phone",
    "memberStatus",
    "vehicleRegistrationPlate",
    "lastInduction",
    "lastSeen",
    "firstJoined",
    "profileUpdateRequired",
    "financial",
    "permissions",
}


def test_the_member_facing_profile_shape(make_member, as_member):
    body = (
        as_member(make_member(state="active", rfid="PROF-1"))
        .get("/api/profile/")
        .json()
    )

    assert set(body) == PROFILE_FIELDS
    assert set(body["financial"]) == {
        "memberBucks",
        "membershipPlan",
        "membershipTier",
        "subscriptionState",
    }
    assert set(body["financial"]["memberBucks"]) == {
        "lastPurchase",
        "balance",
        "savedCard",
    }
    assert body["permissions"] == {"staff": False}


def test_the_two_profile_serializations_disagree(make_member, as_member, as_admin):
    """DEFECT, pinned: the same Profile is published under two different shapes.

    ``Profile.get_basic_profile()`` backs ``/api/admin/members/`` and
    ``ProfileDetail.get()`` backs ``/api/profile/``. They are independent
    hand-built dicts over the same model, and they have drifted: the member's
    own view nests the money under ``financial`` and calls the state
    ``memberStatus``, while the admin view puts it at the top level and calls
    it ``state``. Unifying them is a breaking change for the frontend either
    way, so the disagreement is pinned rather than quietly resolved.
    """
    member = make_member(state="active", rfid="PROF-2")

    own = as_member(member).get("/api/profile/").json()
    listed = as_admin().get("/api/admin/members/").json()
    admin_view = next(m for m in listed if m["id"] == member.user_id)

    assert own["memberStatus"] == "active"
    assert admin_view["state"] == "active"

    assert own["firstName"] == "Test"
    assert admin_view["name"]["first"] == "Test"

    assert "balance" in own["financial"]["memberBucks"]
    assert "balance" in admin_view["memberBucks"]

    assert own["permissions"] == {"staff": False}
    assert admin_view["admin"] is False


def test_updating_your_own_profile(make_member, as_member):
    from profile.models import Profile

    member = make_member(state="active", rfid="PROF-3")

    response = as_member(member).put(
        "/api/profile/",
        {
            "email": member.user.email,
            "firstName": "Renamed",
            "lastName": "Person",
            "phone": "0411222333",
            "screenName": "renamed-screen",
            "vehicleRegistrationPlate": "NEW123",
        },
        format="json",
    )

    assert response.json() == {"success": True}
    refreshed = Profile.objects.get(pk=member.pk)
    assert refreshed.first_name == "Renamed"
    assert refreshed.screen_name == "renamed-screen"


def test_taking_another_members_email_is_a_409(make_member, as_member):
    other = make_member(state="active", rfid="PROF-4")
    member = make_member(state="active", rfid="PROF-5")

    response = as_member(member).put(
        "/api/profile/",
        {
            "email": other.user.email,
            "firstName": "A",
            "lastName": "B",
            "phone": "1",
            "screenName": "unique-screen-name",
            "vehicleRegistrationPlate": "",
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json() == {"message": "error.accountAlreadyExists"}


def test_taking_another_members_screen_name_is_a_409(make_member, as_member):
    other = make_member(state="active", rfid="PROF-6")
    member = make_member(state="active", rfid="PROF-7")

    response = as_member(member).put(
        "/api/profile/",
        {
            "email": member.user.email,
            "firstName": "A",
            "lastName": "B",
            "phone": "1",
            "screenName": other.screen_name,
            "vehicleRegistrationPlate": "",
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json() == {"message": "error.screenNameAlreadyExists"}


def test_changing_your_password(make_member, as_member):
    from profile.models import User

    member = make_member(state="active", rfid="PW-1")

    response = as_member(member).put(
        "/api/profile/password/",
        {"current": PASSWORD, "new": "a-different-password"},
        format="json",
    )

    assert response.json() == {"success": True}
    assert User.objects.get(pk=member.user_id).check_password("a-different-password")


def test_changing_your_password_with_the_wrong_current_one_is_a_403(
    make_member, as_member
):
    from profile.models import User

    member = make_member(state="active", rfid="PW-2")

    response = as_member(member).put(
        "/api/profile/password/",
        {"current": "not-it", "new": "a-different-password"},
        format="json",
    )

    assert response.status_code == 403
    assert User.objects.get(pk=member.user_id).check_password(PASSWORD)


def test_the_digital_id_token_round_trips(make_member, as_member):
    member = make_member(state="active", rfid="DID-1")

    body = as_member(member).get("/api/profile/idtoken/").json()

    assert body["success"] is True
    assert member.validate_digital_id_token(body["token"])


# --------------------------------------------------------------------------
# Site sessions — the input to get_tags()'s sign-in check
# --------------------------------------------------------------------------


def test_signing_into_the_site_creates_a_session(make_member, as_member):
    from api_general.models import SiteSession

    member = make_member(state="active", rfid="SITE-1")

    assert member.is_signed_into_site() is False
    assert as_member(member).post(
        "/api/sitesessions/signin/", {"guests": "[]"}, format="json"
    ).status_code in (200, 201)
    assert member.is_signed_into_site() is True
    assert SiteSession.objects.filter(user=member.user, signout_date=None).count() == 1


def test_signing_in_resyncs_the_members_devices(
    make_member, as_member, make_door, capture_channel_sends
):
    """A sign-in changes who ``get_tags()`` returns, so the doors must be told."""
    door = make_door(serial="site-door")
    member = make_member(state="active", rfid="SITE-2")
    member.doors.add(door)

    as_member(member).post("/api/sitesessions/signin/", {"guests": "[]"}, format="json")

    assert "sync_users" in capture_channel_sends


def test_signing_out_closes_the_session(make_member, as_member):
    member = make_member(state="active", rfid="SITE-3")
    client = as_member(member)
    client.post("/api/sitesessions/signin/", {"guests": "[]"}, format="json")

    client.put("/api/sitesessions/signout/")

    assert member.is_signed_into_site() is False


def test_checking_the_current_site_session(make_member, as_member):
    member = make_member(state="active", rfid="SITE-4")
    client = as_member(member)

    assert client.get("/api/sitesessions/check/").json() is False

    client.post("/api/sitesessions/signin/", {"guests": '["a guest"]'}, format="json")
    body = client.get("/api/sitesessions/check/").json()

    assert body["guests"] == '["a guest"]'
    assert body["signout_date"] is None


def test_signing_in_twice_leaves_two_open_sessions(make_member, as_member):
    """DEFECT, pinned: nothing closes or rejects an existing open session.

    ``SiteSession.objects.create`` is unconditional, so a member who signs in
    twice has two open rows. ``is_signed_into_site()`` only asks whether any
    exist, so access is unaffected — but the occupancy count that the sessions
    exist to provide is now wrong, and one sign-out closes both.
    """
    from api_general.models import SiteSession

    member = make_member(state="active", rfid="SITE-5")
    client = as_member(member)

    client.post("/api/sitesessions/signin/", {"guests": "[]"}, format="json")
    client.post("/api/sitesessions/signin/", {"guests": "[]"}, format="json")

    assert SiteSession.objects.filter(user=member.user, signout_date=None).count() == 2


# --------------------------------------------------------------------------
# Config and permissions
# --------------------------------------------------------------------------


def test_the_config_endpoint_is_public(api_client):
    response = api_client.get("/api/config/")

    assert response.status_code == 200
    assert "general" in response.json()


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/profile/", "get"),
        ("/api/profile/idtoken/", "get"),
        ("/api/sitesessions/check/", "get"),
    ],
)
def test_member_endpoints_reject_anonymous_callers(api_client, path, method):
    assert getattr(api_client, method)(path).status_code == 401


# --------------------------------------------------------------------------
# Kiosks
# --------------------------------------------------------------------------


def test_an_anonymous_caller_cannot_list_kiosks(api_client):
    assert api_client.get("/api/kiosks/").status_code == 403


def test_a_staff_member_can_list_kiosks(as_admin, make_kiosk):
    make_kiosk(kiosk_id="kiosk-list")

    body = as_admin().get("/api/kiosks/").json()

    assert set(body[0]) == {
        "id",
        "name",
        "kioskId",
        "kioskIp",
        "lastSeen",
        "playTheme",
        "authorised",
    }


def test_any_logged_in_member_can_read_the_kiosk_list(
    make_member, as_member, make_kiosk
):
    """DEFECT, pinned: the staff check is inverted, so it only stops anonymous callers.

    All three guards read::

        if not request.user.is_authenticated and not request.user.is_staff:
            return 403

    which blocks a caller who is *neither* authenticated nor staff — that is,
    only an anonymous one. Any logged-in member passes, because the first
    clause is already False. The intent was plainly "unless authenticated
    **and** staff".

    What leaks is the ``kioskId`` of every kiosk, together with whether it is
    authorised. That value is one of the two credentials ``/api/login/kiosk/``
    accepts; the other is a member's raw RFID number. It should not be readable
    by the whole membership.
    """
    make_kiosk(kiosk_id="kiosk-leak")
    member = make_member(state="active", rfid="KIOSK-LEAK")

    body = as_member(member).get("/api/kiosks/").json()

    assert body[0]["kioskId"] == "kiosk-leak"
    assert body[0]["authorised"] is True


def test_any_logged_in_member_can_delete_a_kiosk(make_member, as_member, make_kiosk):
    """DEFECT, pinned: same inverted guard on the destructive path.

    Deleting a kiosk takes it out of service — every terminal that uses that
    id stops being able to log anyone in.
    """
    from api_general.models import Kiosk

    kiosk = make_kiosk(kiosk_id="kiosk-delete")
    member = make_member(state="active", rfid="KIOSK-DEL")

    response = as_member(member).delete(f"/api/kiosks/{kiosk.id}/")

    assert response.status_code == 200
    assert not Kiosk.objects.filter(pk=kiosk.pk).exists()


def test_an_anonymous_caller_can_create_a_kiosk(api_client):
    """DEFECT, pinned: the guard sits inside the ``if id:`` branch.

    A PUT with no id and an unrecognised ``kioskId`` falls through to
    ``Kiosk.objects.create`` without reaching any permission check at all. New
    kiosks are unauthorised by default, so this does not by itself grant a
    login — but it is an unauthenticated write to the table, and the terminal
    self-registration flow it exists for is why the check was skipped.
    """
    from api_general.models import Kiosk

    response = api_client.put(
        "/api/kiosks/", {"kioskId": "self-registered"}, format="json"
    )

    assert response.status_code == 200
    created = Kiosk.objects.get(kiosk_id="self-registered")
    assert created.authorised is False


def test_a_non_staff_member_cannot_change_kiosk_settings(
    make_member, as_member, make_kiosk
):
    """The mutation block, unlike the guards, spells the condition correctly."""
    from api_general.models import Kiosk

    kiosk = make_kiosk(kiosk_id="kiosk-settings", authorised=False)
    member = make_member(state="active", rfid="KIOSK-SET")

    as_member(member).put(
        f"/api/kiosks/{kiosk.id}/",
        {"kioskId": "kiosk-settings", "name": "Renamed", "authorised": True},
        format="json",
    )

    refreshed = Kiosk.objects.get(pk=kiosk.pk)
    assert refreshed.authorised is False
    assert refreshed.name != "Renamed"


def test_a_staff_member_can_change_kiosk_settings(as_admin, make_kiosk):
    from api_general.models import Kiosk

    kiosk = make_kiosk(kiosk_id="kiosk-staff", authorised=False)

    as_admin().put(
        f"/api/kiosks/{kiosk.id}/",
        {"kioskId": "kiosk-staff", "name": "Front Desk", "authorised": True},
        format="json",
    )

    refreshed = Kiosk.objects.get(pk=kiosk.pk)
    assert refreshed.authorised is True
    assert refreshed.name == "Front Desk"


def test_a_kiosk_put_records_the_callers_ip_as_a_checkin(api_client, make_kiosk):
    """Terminals PUT their own id to report they are alive."""
    from api_general.models import Kiosk

    kiosk = make_kiosk(kiosk_id="kiosk-checkin")
    assert kiosk.last_seen is None

    api_client.put(
        f"/api/kiosks/{kiosk.id}/",
        {"kioskId": "kiosk-checkin"},
        format="json",
        REMOTE_ADDR="192.0.2.10",
    )

    refreshed = Kiosk.objects.get(pk=kiosk.pk)
    assert refreshed.last_seen is not None
    assert refreshed.ip_address == "192.0.2.10"
