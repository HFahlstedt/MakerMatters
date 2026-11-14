"""Characterisation: what the billing endpoints do when things go wrong.

``test_billing_subscriptions.py`` and ``test_signup_pipeline.py`` pin the happy
paths: a card is saved, a plan is bought, a subscription is cancelled. This file
is the rest of ``api_billing/views.py``: Stripe refusing or timing out, the
email provider being down, a caller sending something the portal's own form
never would.

It matters more than it looks. These endpoints move a member between Stripe and
the local database in two or three separate steps with no transaction around
them, so *where* a failure lands decides whether the two sides still agree. A
card saved locally that Stripe never attached, or a subscription created in
Stripe that the member is told succeeded but that nothing here recorded, is a
member who is billed for something the portal does not know about.

Nothing here talks to Stripe: ``stripe_stub`` stands in for the SDK.
"""

import pytest
import stripe

from tests.conftest import FakeStripeObject

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def invalid_request(message, code="resource_missing", http_status=None, body=True):
    """A ``stripe.error.InvalidRequestError`` as the SDK builds one."""
    json_body = None
    if body:
        error = {"message": message}
        if code is not None:
            error["code"] = code
        json_body = {"error": error}

    return stripe.error.InvalidRequestError(
        message, param=None, json_body=json_body, http_status=http_status
    )


@pytest.fixture
def billing_member(make_member, make_tier_and_plan, set_config):
    """A member with a Stripe customer and card, optionally on a plan.

    ``with_plan`` gives a plan and ``with_subscription`` the Stripe id that
    normally goes with it; a plan without one is the state an administrator
    leaves a member in when they assign a plan by hand.
    """

    def _build(with_plan=False, with_subscription=False, **overrides):
        set_config(ENABLE_STRIPE=True, **overrides)
        profile = make_member(state="active", rfid="BILL-FAIL")
        profile.stripe_customer_id = "cus_fail"
        profile.stripe_payment_method_id = "pm_fail"
        profile.stripe_card_last_digits = "4242"
        profile.stripe_card_expiry = "01/2030"
        if with_plan:
            _tier, plan = make_tier_and_plan()
            profile.membership_plan = plan
        if with_subscription:
            profile.stripe_subscription_id = "sub_fail"
            profile.subscription_status = "active"
        profile.save()
        return profile

    return _build


@pytest.fixture
def captured(monkeypatch):
    """The exceptions the views hand to Sentry."""
    seen = []
    monkeypatch.setattr("api_billing.views.capture_exception", seen.append)
    return seen


def fresh(profile):
    from profile.models import Profile

    return Profile.objects.get(pk=profile.pk)


def event_log(profile):
    from profile.models import UserEventLog

    return list(UserEventLog.objects.filter(user=profile.user).order_by("pk"))


def subjects(sent_emails):
    return [email["Subject"] for email in sent_emails]


# --------------------------------------------------------------------------
# StripeAPIView: the base class every billing view shares
# --------------------------------------------------------------------------


def test_turning_stripe_off_does_not_stop_the_billing_endpoints(
    billing_member, as_member, stripe_stub, set_config
):
    """Pinned as behaviour: ``ENABLE_STRIPE`` only decides whether the API key is set.

    ``StripeAPIView.__init__`` returns early when the setting is off, which
    skips ``stripe.api_key = ...`` and nothing else. The views still run and
    still call Stripe, so a space that has switched Stripe off keeps offering
    endpoints that fail inside the SDK. Whether they should answer 404
    instead is a decision.
    """
    profile = billing_member()
    set_config(ENABLE_STRIPE=False)
    stripe_stub.set("Customer.retrieve", FakeStripeObject({"id": "cus_fail"}))
    stripe_stub.set("SetupIntent.create", FakeStripeObject({"client_secret": "seti"}))

    response = as_member(profile).get("/api/billing/card/")

    assert response.json() == {"clientSecret": "seti"}
    assert stripe_stub.called("Customer.retrieve")


def test_an_unreadable_secret_key_is_reported_and_the_request_carries_on(
    billing_member, as_member, stripe_stub, captured, monkeypatch
):
    """If the settings database is unreachable the key is simply not set."""
    from constance import config as real_config
    from django.db.utils import OperationalError

    class FlakyConfig:
        def __getattr__(self, name):
            if name == "STRIPE_SECRET_KEY":
                raise OperationalError("settings database is down")
            return getattr(real_config, name)

    profile = billing_member()
    monkeypatch.setattr("api_billing.views.config", FlakyConfig())
    stripe_stub.set("Customer.retrieve", FakeStripeObject({"id": "cus_fail"}))
    stripe_stub.set("SetupIntent.create", FakeStripeObject({"client_secret": "seti"}))

    response = as_member(profile).get("/api/billing/card/")

    assert response.status_code == 200
    assert [type(e).__name__ for e in captured] == ["OperationalError"]


# --------------------------------------------------------------------------
# GET /api/billing/card/: the setup intent
# --------------------------------------------------------------------------


def test_a_customer_stripe_does_not_know_is_forgotten_and_recreated(
    billing_member, as_member, stripe_stub, captured
):
    """A 404 from Stripe clears the stored id, then a new customer is made."""
    profile = billing_member()
    stripe_stub.set(
        "Customer.retrieve",
        invalid_request("No such customer: 'cus_fail'", http_status=404),
    )
    stripe_stub.set("Customer.create", FakeStripeObject({"id": "cus_replacement"}))
    stripe_stub.set("SetupIntent.create", FakeStripeObject({"client_secret": "seti"}))

    response = as_member(profile).get("/api/billing/card/")

    assert response.json() == {"clientSecret": "seti"}
    assert fresh(profile).stripe_customer_id == "cus_replacement"
    assert len(captured) == 1


def test_any_other_error_looking_the_customer_up_is_swallowed(
    billing_member, as_member, stripe_stub, captured
):
    """Pinned as behaviour: only a 404 is treated as "no such customer".

    A 400 from ``Customer.retrieve`` is reported to Sentry and then ignored:
    ``customer_exists`` is still true, so the view goes on to create a setup
    intent for a customer it could not look up.
    """
    profile = billing_member()
    stripe_stub.set(
        "Customer.retrieve",
        invalid_request("Bad request", code=None, http_status=400),
    )
    stripe_stub.set("SetupIntent.create", FakeStripeObject({"client_secret": "seti"}))

    response = as_member(profile).get("/api/billing/card/")

    assert response.json() == {"clientSecret": "seti"}
    assert not stripe_stub.called("Customer.create")
    assert stripe_stub.calls_to("SetupIntent.create")[0][2] == {"customer": "cus_fail"}
    assert fresh(profile).stripe_customer_id == "cus_fail"
    assert len(captured) == 1


def test_stripe_failing_while_a_customer_is_created_is_a_503(
    make_member, as_member, stripe_stub, captured, set_config
):
    set_config(ENABLE_STRIPE=True)
    profile = make_member(state="noob", rfid="BILL-503")
    stripe_stub.set("Customer.create", stripe.error.APIConnectionError("no route"))

    response = as_member(profile).get("/api/billing/card/")

    assert response.status_code == 503
    assert response.json() == {"success": False, "message": "no route"}
    assert len(captured) == 1


def test_a_failure_logged_while_creating_a_customer_stores_the_error(
    make_member, as_member, stripe_stub, set_config
):
    """Used to store the DRF request object instead of the failure.

    ``log_event(description, event_type, data)`` takes the detail to record.
    These two handlers passed ``request``, so the audit entry's ``data`` was the
    text ``<rest_framework.request.Request: GET '/api/billing/card/'>`` and the
    actual failure was not in the log at all.
    """
    set_config(ENABLE_STRIPE=True)
    profile = make_member(state="noob", rfid="BILL-LOG")
    stripe_stub.set("Customer.create", stripe.error.APIConnectionError("no route"))

    as_member(profile).get("/api/billing/card/")

    entry = event_log(profile)[-1]
    assert entry.description == "Unknown stripe while saving payment details."
    assert entry.data == "no route"


def test_an_unexpected_error_creating_a_customer_is_a_503_that_hides_the_cause(
    make_member, as_member, stripe_stub, captured, set_config
):
    set_config(ENABLE_STRIPE=True)
    profile = make_member(state="noob", rfid="BILL-503B")
    stripe_stub.set("Customer.create", ValueError("not a stripe problem"))

    response = as_member(profile).get("/api/billing/card/")

    assert response.status_code == 503
    assert response.json() == {
        "success": False,
        "message": "Unknown error (unrelated to stripe).",
    }
    assert len(captured) == 1


def test_stripe_failing_for_a_known_customer_is_a_503(
    billing_member, as_member, stripe_stub, captured
):
    """Used to raise: only the create-a-customer branch had error handling.

    A member who already had a customer went through the ``else`` branch, where
    ``SetupIntent.create`` was bare, so the outage that was a tidy 503 for a
    new member was a 500 for an existing one.
    """
    profile = billing_member()
    stripe_stub.set("Customer.retrieve", FakeStripeObject({"id": "cus_fail"}))
    stripe_stub.set("SetupIntent.create", stripe.error.APIConnectionError("no route"))

    response = as_member(profile).get("/api/billing/card/")

    assert response.status_code == 503
    assert response.json() == {"success": False, "message": "no route"}
    assert len(captured) == 1


# --------------------------------------------------------------------------
# POST/DELETE /api/billing/card/: saving and removing a card
# --------------------------------------------------------------------------


def a_card():
    return FakeStripeObject(
        {"card": {"last4": "1111", "exp_month": 3, "exp_year": 2031}}
    )


def test_a_card_stripe_refuses_to_attach_is_not_recorded_locally(
    billing_member, as_member, stripe_stub, captured
):
    """Used to be saved first: the profile was written before Stripe was asked.

    ``post`` wrote the last four digits, the expiry and the payment method id to
    the profile and only then called ``PaymentMethod.attach``. When Stripe
    refused -- the card belongs to another customer, say -- the request was a
    500, but the account showed a card that existed nowhere on the member's
    Stripe customer, and every later charge failed against it. Stripe is now
    asked first, a refusal is a 400 carrying Stripe's message, and the profile
    keeps the card it had.
    """
    profile = billing_member()
    stripe_stub.set("PaymentMethod.retrieve", a_card())
    stripe_stub.set(
        "PaymentMethod.attach",
        invalid_request("The payment method belongs to another customer."),
    )

    response = as_member(profile).post(
        "/api/billing/card/", {"paymentMethodId": "pm_new"}
    )

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "message": "The payment method belongs to another customer.",
    }
    saved = fresh(profile)
    assert (saved.stripe_card_last_digits, saved.stripe_card_expiry) == (
        "4242",
        "01/2030",
    )
    assert saved.stripe_payment_method_id == "pm_fail"
    assert not stripe_stub.called("Customer.modify")
    assert len(captured) == 1


def test_stripe_being_unreachable_while_saving_a_card_is_a_503_and_changes_nothing(
    billing_member, as_member, stripe_stub, captured
):
    profile = billing_member()
    stripe_stub.set("PaymentMethod.retrieve", stripe.error.APIConnectionError("no route"))

    response = as_member(profile).post(
        "/api/billing/card/", {"paymentMethodId": "pm_new"}
    )

    assert response.status_code == 503
    assert fresh(profile).stripe_payment_method_id == "pm_fail"
    assert len(captured) == 1


@pytest.mark.parametrize(
    "stripe_status, member_status", [("active", "active"), ("trialing", "active")]
)
def test_a_subscription_in_good_standing_is_recorded_and_the_member_activated(
    billing_member, as_member, make_tier_and_plan, stripe_stub, stripe_status, member_status
):
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_new", "status": stripe_status}),
    )

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.json() == {"success": True}
    saved = fresh(profile)
    assert saved.membership_plan_id == plan.id
    assert saved.stripe_subscription_id == "sub_new"
    assert saved.subscription_status == member_status


@pytest.mark.parametrize("stripe_status", ["past_due", "unpaid"])
def test_a_live_subscription_awaiting_payment_is_recorded_but_not_activated(
    billing_member, as_member, make_tier_and_plan, stripe_stub, stripe_status
):
    """Used to be orphaned: created in Stripe, never recorded, and billable twice.

    Only ``"active"`` recorded anything, so the subscription existed in Stripe
    while the profile had no plan and no subscription id, and the member told
    ``success: true`` could sign up again for a second one. It is now recorded
    with the member left inactive; the ``invoice.paid`` webhook activates them
    when the payment lands, and a second signup is a 409.
    """
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_wait", "status": stripe_status}),
    )

    first = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")
    second = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert first.json() == {"success": True}
    assert second.status_code == 409
    saved = fresh(profile)
    assert (saved.membership_plan_id, saved.stripe_subscription_id) == (plan.id, "sub_wait")
    assert saved.subscription_status == "inactive"
    assert len(stripe_stub.calls_to("Subscription.create")) == 1


def test_an_incomplete_subscription_is_cancelled_so_a_retry_starts_clean(
    billing_member, as_member, make_tier_and_plan, stripe_stub
):
    """Used to be orphaned, and reported as ``success: true``.

    The first payment did not complete. The subscription was left in Stripe and
    the member told ``success: true`` with a failure message the frontend
    ignores. It is now cancelled in Stripe and reported as the failure it is,
    so the member sees the retry-friendly dialog and a second attempt cannot
    stack a second subscription.
    """
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_inc", "status": "incomplete"}),
    )

    first = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")
    second = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    expected = {"success": False, "message": "signup.subscriptionFailed"}
    assert first.json() == second.json() == expected
    assert [c[1] for c in stripe_stub.calls_to("Subscription.cancel")] == [
        ("sub_inc",),
        ("sub_inc",),
    ]
    saved = fresh(profile)
    assert (saved.membership_plan, saved.stripe_subscription_id) == (None, "")


def test_a_failure_cancelling_an_abandoned_subscription_is_reported_not_raised(
    billing_member, as_member, make_tier_and_plan, stripe_stub, captured
):
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_inc", "status": "incomplete"}),
    )
    stripe_stub.set("Subscription.cancel", stripe.error.APIConnectionError("no route"))

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.json() == {"success": False, "message": "signup.subscriptionFailed"}
    assert len(captured) == 1
    assert event_log(profile)[-1].data == "sub_inc"


@pytest.mark.parametrize("stripe_status", ["canceled", "incomplete_expired", "paused"])
def test_a_subscription_that_is_not_live_is_a_failure_and_records_nothing(
    billing_member, as_member, make_tier_and_plan, stripe_stub, stripe_status
):
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_dead", "status": stripe_status}),
    )

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.json() == {"success": False, "message": "signup.subscriptionFailed"}
    assert not stripe_stub.called("Subscription.cancel")
    assert fresh(profile).membership_plan is None


# --------------------------------------------------------------------------
# GET /api/billing/myplan/: subscription details
# --------------------------------------------------------------------------


def test_an_empty_answer_from_stripe_is_reported_as_no_subscription(
    billing_member, as_member, stripe_stub
):
    profile = billing_member(with_plan=True, with_subscription=True)
    stripe_stub.set("Subscription.retrieve", FakeStripeObject())

    response = as_member(profile).get("/api/billing/myplan/")

    assert response.json() == {"success": False}


def test_a_subscription_stripe_no_longer_has_is_reported_as_none(
    billing_member, as_member, stripe_stub, captured
):
    """Used to raise: ``Subscription.retrieve`` was bare.

    The local ``stripe_subscription_id`` is only cleared by the
    ``customer.subscription.deleted`` webhook. If that is missed, or the
    subscription is deleted in the Stripe dashboard first, every visit to the
    page that calls this endpoint was a 500.
    """
    profile = billing_member(with_plan=True, with_subscription=True)
    stripe_stub.set(
        "Subscription.retrieve",
        invalid_request("No such subscription: 'sub_fail'", http_status=404),
    )

    response = as_member(profile).get("/api/billing/myplan/")

    assert response.json() == {"success": False}
    assert len(captured) == 1


def test_stripe_being_unreachable_loading_the_plan_page_is_a_503(
    billing_member, as_member, stripe_stub, captured
):
    profile = billing_member(with_plan=True, with_subscription=True)
    stripe_stub.set("Subscription.retrieve", stripe.error.APIConnectionError("no route"))

    response = as_member(profile).get("/api/billing/myplan/")

    assert response.status_code == 503
    assert response.json() == {"success": False}


# --------------------------------------------------------------------------
# POST /api/billing/myplan/<resume|cancel>/
# --------------------------------------------------------------------------


def test_the_cancel_word_cancels_the_plan(
    billing_member, as_member, stripe_stub, sent_emails
):
    profile = billing_member(with_plan=True, with_subscription=True)
    stripe_stub.set(
        "Subscription.modify", FakeStripeObject({"cancel_at_period_end": True})
    )

    response = as_member(profile).post("/api/billing/myplan/cancel/")

    assert response.json() == {"success": True}
    assert stripe_stub.calls_to("Subscription.modify")[0][2] == {
        "cancel_at_period_end": True
    }
    assert fresh(profile).subscription_status == "cancelling"


@pytest.mark.parametrize("word", ["pause", "resumed", "Resume", "Cancel", "x"])
def test_any_other_word_is_a_400_and_touches_nothing(
    billing_member, as_member, stripe_stub, sent_emails, word
):
    """Used to cancel the plan: only exactly "resume" was special.

    ``resume = True if resume == "resume" else False``. The frontend sends
    "cancel" or "resume", but a typo, a different capitalisation, or a request
    made by hand to ``/api/billing/myplan/anything/`` scheduled the cancellation
    of the member's subscription in Stripe. Only the two real words are accepted.
    """
    profile = billing_member(with_plan=True, with_subscription=True)

    response = as_member(profile).post(f"/api/billing/myplan/{word}/")

    assert response.status_code == 400
    assert response.json() == {"success": False, "message": "paymentPlan.invalidAction"}
    assert not stripe_stub.called("Subscription.modify")
    assert fresh(profile).subscription_status == "active"
    assert sent_emails == []


def test_cancelling_a_plan_with_no_stripe_subscription_is_a_404_and_asks_stripe_nothing(
    billing_member, as_member, stripe_stub
):
    """Used to ask Stripe to modify subscription ``""``: only *resume* checked the id.

    An administrator can give a member a plan by hand, and a subscription that
    was never recorded leaves the same state. Cancelling then called
    ``Subscription.modify("", ...)`` (the column defaults to the empty string,
    not null), which Stripe rejects, and the member saw a 500 for a plan they
    were never being billed for.
    """
    profile = billing_member(with_plan=True, with_subscription=False)

    response = as_member(profile).post("/api/billing/myplan/cancel/")

    assert response.status_code == 404
    assert response.json() == {"success": False, "message": "paymentPlan.notExists"}
    assert not stripe_stub.called("Subscription.modify")


def test_a_cancellation_stripe_does_not_confirm_is_reported_to_the_committee(
    billing_member, as_member, stripe_stub, sent_emails
):
    profile = billing_member(with_plan=True, with_subscription=True)
    stripe_stub.set(
        "Subscription.modify", FakeStripeObject({"cancel_at_period_end": False})
    )

    response = as_member(profile).post("/api/billing/myplan/cancel/")

    # A failure is a 200 with success false; the member is not emailed.
    assert response.status_code == 200
    assert response.json() == {"success": False}
    assert fresh(profile).subscription_status == "active"
    assert subjects(sent_emails) == [
        "Test Member1 requested to cancel their membership plan but it failed."
    ]
    assert event_log(profile)[-1].description.endswith("but it failed.")


def test_a_resume_stripe_does_not_confirm_is_reported_to_the_committee(
    billing_member, as_member, stripe_stub, sent_emails
):
    profile = billing_member(with_plan=True, with_subscription=True)
    profile.subscription_status = "cancelling"
    profile.save()
    stripe_stub.set(
        "Subscription.modify", FakeStripeObject({"cancel_at_period_end": True})
    )

    response = as_member(profile).post("/api/billing/myplan/resume/")

    assert response.status_code == 200
    assert response.json() == {"success": False}
    assert fresh(profile).subscription_status == "cancelling"
    assert subjects(sent_emails) == [
        "Test Member1 tried to resume their cancelling membership plan but it failed."
    ]


def test_restarting_a_lapsed_plan_passes_a_stripe_failure_straight_through(
    billing_member, as_member, stripe_stub
):
    """Resuming with no subscription creates one; its error Response is returned."""
    profile = billing_member(with_plan=True, with_subscription=False)
    stripe_stub.set("Subscription.create", stripe.error.APIConnectionError("no route"))

    response = as_member(profile).post("/api/billing/myplan/resume/")

    assert response.status_code == 500
    assert response.json() == {"success": False, "message": None}


@pytest.mark.parametrize("stripe_status", ["active", "trialing", "past_due"])
def test_restarting_a_lapsed_plan_records_the_new_subscription(
    billing_member, as_member, stripe_stub, sent_emails, stripe_status
):
    """Used to share the signup defect: only ``"active"`` recorded the id, so
    the member was told ``success: true`` for a subscription the profile knew
    nothing about, and resuming again created another."""
    profile = billing_member(with_plan=True, with_subscription=False)
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_again", "status": stripe_status}),
    )
    stripe_stub.set(
        "Subscription.modify", FakeStripeObject({"cancel_at_period_end": False})
    )

    response = as_member(profile).post("/api/billing/myplan/resume/")
    as_member(profile).post("/api/billing/myplan/resume/")

    assert response.json() == {"success": True}
    assert fresh(profile).stripe_subscription_id == "sub_again"
    # The second resume finds a subscription and asks Stripe to un-cancel it.
    assert len(stripe_stub.calls_to("Subscription.create")) == 1
    assert stripe_stub.called("Subscription.modify")


def test_restarting_a_lapsed_plan_whose_payment_fails_is_a_failure(
    billing_member, as_member, stripe_stub
):
    profile = billing_member(with_plan=True, with_subscription=False)
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_inc", "status": "incomplete"}),
    )

    response = as_member(profile).post("/api/billing/myplan/resume/")

    assert response.json() == {"success": False, "message": "signup.subscriptionFailed"}
    assert fresh(profile).stripe_subscription_id == ""
    assert stripe_stub.called("Subscription.cancel")


# --------------------------------------------------------------------------
# POST /api/billing/check-induction/
# --------------------------------------------------------------------------


def induction_needed(make_member, set_config, **settings):
    set_config(MAX_INDUCTION_DAYS=180, **settings)
    return make_member(state="noob", rfid="BILL-IND")


def test_an_unreachable_canvas_is_reported_as_a_failed_check(
    make_member, as_member, set_config, monkeypatch, captured
):
    from django.db.utils import OperationalError

    profile = induction_needed(
        make_member,
        set_config,
        MOODLE_INDUCTION_ENABLED=False,
        CANVAS_INDUCTION_ENABLED=True,
    )

    def _down():
        raise OperationalError("settings database is down")

    monkeypatch.setattr("api_billing.views.Canvas", _down)

    response = as_member(profile).post("/api/billing/check-induction/")

    assert response.json() == {"success": False, "score": 0}
    assert len(captured) == 1


def test_a_moodle_course_with_no_percentage_counts_as_zero(
    make_member, as_member, set_config, monkeypatch, captured
):
    """Used to leak a Python error to the member.

    With a minimum score of 0, a Moodle answer whose ``percentage_completed`` is
    ``None`` reached ``None >= 0`` and raised ``TypeError``, which the catch-all
    put, as the interpreter's own message, in the JSON the member's browser
    received. A missing percentage now counts as 0, as a missing Canvas score
    already did, and the catch-all no longer returns the exception text.
    """
    profile = induction_needed(
        make_member,
        set_config,
        MOODLE_INDUCTION_ENABLED=True,
        CANVAS_INDUCTION_ENABLED=False,
        MIN_INDUCTION_SCORE=0,
    )
    monkeypatch.setattr(
        "api_billing.views.moodle_get_user_from_email", lambda email: {"id": 42}
    )
    monkeypatch.setattr(
        "api_billing.views.moodle_get_course_activity_completion_status",
        lambda course_id, user_id: {"percentage_completed": None},
    )

    response = as_member(profile).post("/api/billing/check-induction/")

    assert response.json() == {"success": True, "score": 0}
    assert captured == []


def test_an_unexpected_failure_checking_the_induction_does_not_leak_its_message(
    make_member, as_member, set_config, monkeypatch, captured
):
    profile = induction_needed(
        make_member,
        set_config,
        MOODLE_INDUCTION_ENABLED=True,
        CANVAS_INDUCTION_ENABLED=False,
        MIN_INDUCTION_SCORE=50,
    )
    monkeypatch.setattr(
        "api_billing.views.moodle_get_user_from_email", lambda email: {"id": 42}
    )
    monkeypatch.setattr(
        "api_billing.views.moodle_get_course_activity_completion_status",
        lambda course_id, user_id: {"percentage_completed": 100},
    )

    def _boom(self):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("profile.models.Profile.update_last_induction", _boom)

    response = as_member(profile).post("/api/billing/check-induction/")

    assert response.json() == {"success": False, "score": 0}
    assert len(captured) == 1


# --------------------------------------------------------------------------
# POST /api/billing/access-card/
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", [{}, {"accessCard": ""}, {"accessCard": "   "}, {"accessCard": None}, {"accessCard": 12}])
def test_assigning_a_missing_or_blank_card_is_a_400_and_keeps_the_old_one(
    make_member, as_member, body
):
    """Used to raise ``KeyError`` for a missing field, and to store ``""`` for a blank.

    ``rfid`` is ``unique=True, null=True``: any number of members can have no
    card (null), but not two with an empty string. A blank skipped the in-use
    check, so the first member to submit one had any card they held wiped, and
    the second got an ``IntegrityError`` and a 500.
    """
    profile = make_member(state="noob", rfid="BILL-CARD")

    response = as_member(profile).post("/api/billing/access-card/", body, format="json")

    assert response.status_code == 400
    assert response.json() == {"success": False, "error": "accessCardRequired"}
    assert fresh(profile).rfid == "BILL-CARD"


def test_a_card_is_stored_without_surrounding_whitespace(make_member, as_member):
    profile = make_member(state="noob", rfid=None)

    response = as_member(profile).post(
        "/api/billing/access-card/", {"accessCard": "  PADDED-1  "}, format="json"
    )

    assert response.json() == {"success": True}
    assert fresh(profile).rfid == "PADDED-1"
