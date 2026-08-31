"""Characterisation: the member signup pipeline.

Signup is split across two apps — ``api_general`` does registration and email
verification, ``api_billing`` does everything after — with the gating logic
living in ``Profile.can_signup()``. There are two independent routes to an
active membership: a member completing the flow themselves, and Stripe's
``invoice.paid`` webhook activating them asynchronously (covered in
test_stripe_webhook.py). Both write the same fields.

This is the highest business-risk code in the system: it decides who becomes a
member and who gets physical site access.
"""

import pytest

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# can_signup() — the gate everything else consults
# --------------------------------------------------------------------------


def test_a_member_with_induction_and_card_can_signup(make_member, set_config):
    set_config(MAX_INDUCTION_DAYS=180, CANVAS_INDUCTION_ENABLED=True)
    profile = make_member(state="noob", rfid="TAG-SIGNUP")
    profile.update_last_induction()

    assert profile.can_signup() == {"success": True, "requiredSteps": []}


def test_missing_access_card_blocks_signup(make_member, set_config):
    set_config(MAX_INDUCTION_DAYS=0)
    profile = make_member(state="noob", rfid=None)

    assert profile.can_signup() == {"success": False, "requiredSteps": ["accessCard"]}


def test_missing_induction_blocks_signup(make_member, set_config):
    set_config(MAX_INDUCTION_DAYS=180, CANVAS_INDUCTION_ENABLED=True)
    profile = make_member(state="noob", rfid="TAG-SIGNUP")

    assert profile.can_signup() == {"success": False, "requiredSteps": ["induction"]}


def test_a_stale_induction_blocks_signup(make_member, set_config):
    """Inductions expire after MAX_INDUCTION_DAYS, for returning members."""
    from datetime import timedelta

    from django.utils import timezone

    set_config(MAX_INDUCTION_DAYS=30, CANVAS_INDUCTION_ENABLED=True)
    profile = make_member(state="noob", rfid="TAG-SIGNUP")
    profile.last_induction = timezone.now() - timedelta(days=31)
    profile.save()

    assert profile.can_signup()["requiredSteps"] == ["induction"]


def test_induction_is_not_required_when_no_lms_is_configured(make_member, set_config):
    """MAX_INDUCTION_DAYS alone does nothing — a Canvas or Moodle course must be enabled."""
    set_config(
        MAX_INDUCTION_DAYS=180,
        CANVAS_INDUCTION_ENABLED=False,
        MOODLE_INDUCTION_ENABLED=False,
    )
    profile = make_member(state="noob", rfid="TAG-SIGNUP")

    assert profile.can_signup()["success"] is True


def test_both_requirements_are_reported_together(make_member, set_config):
    set_config(MAX_INDUCTION_DAYS=180, CANVAS_INDUCTION_ENABLED=True)
    profile = make_member(state="noob", rfid=None)

    assert profile.can_signup()["requiredSteps"] == ["induction", "accessCard"]


def test_can_signup_endpoint_reports_outstanding_steps(
    make_member, as_member, set_config
):
    set_config(MAX_INDUCTION_DAYS=0)
    profile = make_member(state="noob", rfid=None)

    response = as_member(profile).get("/api/billing/can-signup/")

    assert response.status_code == 200
    assert response.json() == {"success": False, "requiredSteps": ["accessCard"]}


# --------------------------------------------------------------------------
# Access card assignment
# --------------------------------------------------------------------------


def test_assigning_an_access_card_stores_the_tag(make_member, as_member):
    from profile.models import Profile

    profile = make_member(state="noob", rfid=None)

    response = as_member(profile).post(
        "/api/billing/access-card/", {"accessCard": "NEW-TAG-123"}, format="json"
    )

    assert response.status_code == 200
    assert Profile.objects.get(pk=profile.pk).rfid == "NEW-TAG-123"


def test_assigning_a_card_already_held_by_another_member_is_rejected(
    make_member, as_member
):
    """``Profile.rfid`` is unique, so the clash used to be an unhandled 500.

    DEFECT, still pinned: a tag that IS free is accepted with no verification
    that the member actually holds that card, so anyone can claim any unused
    tag number. Fixing that needs a verification flow (swipe the card at a
    reader), not a uniqueness check.
    """
    from profile.models import Profile

    make_member(state="active", rfid="TAKEN-TAG")
    newcomer = make_member(state="noob", rfid=None)

    response = as_member(newcomer).post(
        "/api/billing/access-card/", {"accessCard": "TAKEN-TAG"}, format="json"
    )

    assert response.status_code == 400
    assert response.json() == {"success": False, "error": "accessCardInUse"}
    assert Profile.objects.get(pk=newcomer.pk).rfid is None


def test_resubmitting_the_card_a_member_already_holds_is_allowed(
    make_member, as_member
):
    """The clash check must not treat the member's own tag as taken."""
    member = make_member(state="active", rfid="MY-OWN-TAG")

    response = as_member(member).post(
        "/api/billing/access-card/", {"accessCard": "MY-OWN-TAG"}, format="json"
    )

    assert response.status_code == 200


# --------------------------------------------------------------------------
# Induction checking
# --------------------------------------------------------------------------


def test_induction_check_short_circuits_when_not_required(
    make_member, as_member, set_config
):
    set_config(MAX_INDUCTION_DAYS=0)
    profile = make_member(state="noob", rfid="TAG-IND")

    response = as_member(profile).post("/api/billing/check-induction/")

    assert response.json() == {"success": True, "score": 0, "notRequired": True}


def test_passing_moodle_induction_records_the_completion(
    make_member, as_member, set_config, monkeypatch
):
    from profile.models import Profile

    set_config(
        MAX_INDUCTION_DAYS=180,
        MOODLE_INDUCTION_ENABLED=True,
        CANVAS_INDUCTION_ENABLED=False,
        MIN_INDUCTION_SCORE=80,
    )
    profile = make_member(state="noob", rfid="TAG-IND")

    monkeypatch.setattr(
        "api_billing.views.moodle_get_user_from_email", lambda email: {"id": 42}
    )
    monkeypatch.setattr(
        "api_billing.views.moodle_get_course_activity_completion_status",
        lambda course_id, user_id: {"percentage_completed": 100},
    )

    response = as_member(profile).post("/api/billing/check-induction/")

    assert response.json() == {"success": True, "score": 100}
    assert Profile.objects.get(pk=profile.pk).last_induction is not None


def test_failing_moodle_induction_does_not_record_completion(
    make_member, as_member, set_config, monkeypatch
):
    from profile.models import Profile

    set_config(
        MAX_INDUCTION_DAYS=180,
        MOODLE_INDUCTION_ENABLED=True,
        CANVAS_INDUCTION_ENABLED=False,
        MIN_INDUCTION_SCORE=80,
    )
    profile = make_member(state="noob", rfid="TAG-IND")

    monkeypatch.setattr(
        "api_billing.views.moodle_get_user_from_email", lambda email: {"id": 42}
    )
    monkeypatch.setattr(
        "api_billing.views.moodle_get_course_activity_completion_status",
        lambda course_id, user_id: {"percentage_completed": 50},
    )

    response = as_member(profile).post("/api/billing/check-induction/")

    assert response.json() == {"success": False, "score": 50}
    assert Profile.objects.get(pk=profile.pk).last_induction is None


def test_passing_canvas_induction_records_the_completion(
    make_member, as_member, set_config, monkeypatch
):
    from profile.models import Profile

    set_config(
        MAX_INDUCTION_DAYS=180,
        CANVAS_INDUCTION_ENABLED=True,
        MOODLE_INDUCTION_ENABLED=False,
        MIN_INDUCTION_SCORE=80,
    )
    profile = make_member(state="noob", rfid="TAG-IND")

    monkeypatch.setattr(
        "api_billing.views.Canvas",
        lambda: type(
            "C", (), {"get_student_score_for_course": lambda self, c, e: 95}
        )(),
    )

    response = as_member(profile).post("/api/billing/check-induction/")

    assert response.json() == {"success": True, "score": 95}
    assert Profile.objects.get(pk=profile.pk).last_induction is not None


# --------------------------------------------------------------------------
# Completing signup
# --------------------------------------------------------------------------


@pytest.fixture
def ready_to_complete(make_member, set_config):
    """A member who has met every requirement and holds an active subscription."""

    def _build(**overrides):
        set_config(MAX_INDUCTION_DAYS=0, **overrides)
        profile = make_member(state="noob", rfid="TAG-COMPLETE")
        profile.subscription_status = "active"
        profile.save()
        return profile

    return _build


def test_completing_signup_activates_the_member(
    ready_to_complete, as_member, sent_emails
):
    from profile.models import Profile

    profile = ready_to_complete()

    response = as_member(profile).post("/api/billing/complete-signup/")

    assert response.json() == {"success": True}
    assert Profile.objects.get(pk=profile.pk).state == "active"


def test_completing_signup_grants_every_default_access_device(
    ready_to_complete, as_member, make_door, make_interlock, sent_emails
):
    from profile.models import Profile

    default_door = make_door(serial="door-default", all_members=True)
    other_door = make_door(serial="door-opt-in", all_members=False)
    default_interlock = make_interlock(serial="int-default", all_members=True)

    profile = ready_to_complete()
    as_member(profile).post("/api/billing/complete-signup/")

    refreshed = Profile.objects.get(pk=profile.pk)
    assert list(refreshed.doors.values_list("id", flat=True)) == [default_door.id]
    assert other_door.id not in refreshed.doors.values_list("id", flat=True)
    assert list(refreshed.interlocks.values_list("id", flat=True)) == [
        default_interlock.id
    ]


def test_completing_signup_emails_the_member_and_the_committee(
    ready_to_complete, as_member, sent_emails
):
    """Three messages: application confirmation, committee notification, welcome."""
    profile = ready_to_complete()

    as_member(profile).post("/api/billing/complete-signup/")

    subjects = [email["Subject"] for email in sent_emails]
    assert len(sent_emails) == 3
    assert "Your membership application has been submitted" in subjects
    assert any("just became a member applicant" in s for s in subjects)
    assert any(s.startswith("Welcome to") for s in subjects)


def test_completing_signup_without_a_subscription_is_refused(
    make_member, as_member, set_config
):
    from profile.models import Profile

    set_config(MAX_INDUCTION_DAYS=0)
    profile = make_member(state="noob", rfid="TAG-NOSUB")

    response = as_member(profile).post("/api/billing/complete-signup/")

    assert response.json()["success"] is False
    assert response.json()["message"] == "signup.requirementsNotMet"
    assert response.json()["items"] == ["No active subscription found."]
    assert Profile.objects.get(pk=profile.pk).state == "noob"


def test_completing_signup_with_outstanding_steps_is_refused(
    make_member, as_member, set_config
):
    from profile.models import Profile

    set_config(MAX_INDUCTION_DAYS=0)
    profile = make_member(state="noob", rfid=None)
    profile.subscription_status = "active"
    profile.save()

    response = as_member(profile).post("/api/billing/complete-signup/")

    assert response.json()["success"] is False
    assert response.json()["items"] == ["accessCard"]
    assert Profile.objects.get(pk=profile.pk).state == "noob"


def test_skipping_signup_leaves_an_account_only_member(make_member, as_member):
    from profile.models import Profile

    profile = make_member(state="noob", rfid=None)

    response = as_member(profile).post("/api/billing/skip-signup/")

    assert response.json() == {"success": True}
    assert Profile.objects.get(pk=profile.pk).state == "accountonly"


# --------------------------------------------------------------------------
# Activation / deactivation side effects
# --------------------------------------------------------------------------


def test_activating_an_established_member_notifies_them(make_member, sent_emails):
    """A 'noob' is activated silently; anyone else gets an email and an SMS."""
    profile = make_member(state="inactive", rfid="TAG-ACT")

    profile.activate()

    assert profile.state == "active"
    assert len(sent_emails) == 1
    assert "site access has been enabled" in sent_emails[0]["Subject"]


def test_activating_a_new_member_is_silent(make_member, sent_emails):
    profile = make_member(state="noob", rfid="TAG-ACT")

    profile.activate()

    assert profile.state == "active"
    assert sent_emails == []


def test_deactivating_a_member_notifies_them(make_member, sent_emails):
    profile = make_member(state="active", rfid="TAG-DEACT")

    profile.deactivate()

    assert profile.state == "inactive"
    assert len(sent_emails) == 1
    assert "disabled" in sent_emails[0]["Subject"]


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def test_registration_creates_a_user_and_profile(api_client, sent_emails):
    from api_general.models import EmailVerificationToken
    from profile.models import Profile, User

    response = api_client.post(
        "/api/register/",
        {
            "email": "Newcomer@Example.com",
            "password": "a-sufficiently-long-passphrase",
            "firstName": "New",
            "lastName": "Comer",
            "screenName": "newcomer",
            "mobile": "0400000000",
        },
        format="json",
    )

    assert response.status_code == 200

    user = User.objects.get(email="newcomer@example.com")  # normalised to lowercase
    assert user.email_verified is False
    assert Profile.objects.get(user=user).state == "noob"
    assert EmailVerificationToken.objects.filter(user=user).count() == 1


def test_registration_rejects_a_duplicate_email(make_member, api_client):
    existing = make_member()

    response = api_client.post(
        "/api/register/",
        {
            "email": existing.user.email,
            "password": "a-sufficiently-long-passphrase",
            "firstName": "Dup",
            "lastName": "Licate",
            "screenName": "totally-different",
            "mobile": "0400000000",
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["message"] == "error.accountAlreadyExists"


def test_registration_rejects_a_duplicate_screen_name(make_member, api_client):
    existing = make_member()

    response = api_client.post(
        "/api/register/",
        {
            "email": "someone-else@example.com",
            "password": "a-sufficiently-long-passphrase",
            "firstName": "Dup",
            "lastName": "Licate",
            "screenName": existing.screen_name,
            "mobile": "0400000000",
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["message"] == "error.screenNameAlreadyExists"


def test_verifying_an_email_marks_the_user_and_consumes_the_token(
    make_member, api_client
):
    from api_general.models import EmailVerificationToken
    from profile.models import User

    profile = make_member()
    profile.user.email_verified = False
    profile.user.save()
    token = EmailVerificationToken.objects.create(user=profile.user)

    response = api_client.post(f"/api/email/{token.verification_token}/verify/")

    assert response.status_code == 200
    assert User.objects.get(pk=profile.user_id).email_verified is True
    assert EmailVerificationToken.objects.count() == 0


def test_verifying_with_an_unknown_token_is_rejected(api_client):
    import uuid

    response = api_client.post(f"/api/email/{uuid.uuid4()}/verify/")

    assert response.status_code == 401
    assert response.json()["message"] == "error.emailVerificationFailed"


def test_an_expired_verification_token_is_replaced(
    make_member, api_client, sent_emails
):
    """The old token is consumed and a fresh one emailed, rather than failing outright."""
    from datetime import timedelta

    from django.utils import timezone

    from api_general.models import EmailVerificationToken

    profile = make_member()
    profile.user.email_verified = False
    profile.user.save()
    token = EmailVerificationToken.objects.create(user=profile.user)
    EmailVerificationToken.objects.filter(pk=token.pk).update(
        creation_date=timezone.now() - timedelta(hours=25)
    )

    response = api_client.post(f"/api/email/{token.verification_token}/verify/")

    assert response.status_code == 403
    assert response.json()["message"] == "error.emailVerificationExpired"

    remaining = EmailVerificationToken.objects.all()
    assert remaining.count() == 1
    assert remaining.first().verification_token != token.verification_token
    assert len(sent_emails) == 1
