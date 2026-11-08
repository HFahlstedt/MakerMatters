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


def test_a_failure_logged_while_creating_a_customer_stores_the_request_not_the_error(
    make_member, as_member, stripe_stub, set_config
):
    """DEFECT, pinned: the third argument to ``log_event`` is ``request``.

    ``log_event(description, event_type, data)`` takes the detail to record,
    which the neighbouring handlers pass as the error. These two pass the DRF
    request object, so the audit entry's ``data`` is the text
    ``<rest_framework.request.Request: GET '/api/billing/card/'>`` and the
    actual failure is not in the log at all.
    """
    set_config(ENABLE_STRIPE=True)
    profile = make_member(state="noob", rfid="BILL-LOG")
    stripe_stub.set("Customer.create", stripe.error.APIConnectionError("no route"))

    as_member(profile).get("/api/billing/card/")

    entry = event_log(profile)[-1]
    assert entry.description == "Unknown stripe while saving payment details."
    assert entry.data.startswith("<rest_framework.request.Request")
    assert "no route" not in entry.data


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


def test_stripe_failing_for_a_known_customer_is_an_unhandled_error(
    billing_member, as_member, stripe_stub
):
    """DEFECT, pinned: only the create-a-customer branch has error handling.

    A member who already has a customer goes through the ``else`` branch, where
    ``SetupIntent.create`` is bare. The same outage that is a tidy 503 for a
    new member is an exception, and a 500, for an existing one.
    """
    profile = billing_member()
    stripe_stub.set("Customer.retrieve", FakeStripeObject({"id": "cus_fail"}))
    stripe_stub.set("SetupIntent.create", stripe.error.APIConnectionError("no route"))

    with pytest.raises(stripe.error.APIConnectionError):
        as_member(profile).get("/api/billing/card/")


# --------------------------------------------------------------------------
# POST/DELETE /api/billing/card/: saving and removing a card
# --------------------------------------------------------------------------


def a_card():
    return FakeStripeObject(
        {"card": {"last4": "1111", "exp_month": 3, "exp_year": 2031}}
    )


def test_a_card_stripe_refuses_to_attach_is_still_recorded_locally(
    billing_member, as_member, stripe_stub
):
    """DEFECT, pinned: the profile is saved before Stripe is asked to attach.

    ``post`` writes the last four digits, the expiry and the payment method id
    to the profile and only then calls ``PaymentMethod.attach``. When Stripe
    refuses -- the card belongs to another customer, say -- the request is a
    500, but the member's account now shows a card that exists nowhere on
    their Stripe customer, and every later charge fails against it.
    """
    profile = billing_member()
    stripe_stub.set("PaymentMethod.retrieve", a_card())
    stripe_stub.set(
        "PaymentMethod.attach",
        invalid_request("The payment method belongs to another customer."),
    )

    with pytest.raises(stripe.error.InvalidRequestError):
        as_member(profile).post("/api/billing/card/", {"paymentMethodId": "pm_new"})

    saved = fresh(profile)
    assert (saved.stripe_card_last_digits, saved.stripe_card_expiry) == (
        "1111",
        "03/2031",
    )
    assert saved.stripe_payment_method_id == "pm_new"
    assert not stripe_stub.called("Customer.modify")


def test_saving_a_card_without_a_payment_method_id_is_an_unhandled_error(
    billing_member, as_member, stripe_stub
):
    """DEFECT, pinned: nothing checks the body; Stripe is asked about ``None``."""
    profile = billing_member()
    stripe_stub.set(
        "PaymentMethod.retrieve",
        invalid_request(
            "Missing required param: payment_method.", code="parameter_missing"
        ),
    )

    with pytest.raises(stripe.error.InvalidRequestError):
        as_member(profile).post("/api/billing/card/", {})

    assert stripe_stub.calls_to("PaymentMethod.retrieve")[0][1] == (None,)
    assert fresh(profile).stripe_card_last_digits == "4242"


def test_a_failed_confirmation_email_turns_a_saved_card_into_a_500(
    billing_member, as_member, stripe_stub, captured, monkeypatch
):
    """Pinned as behaviour: the card is saved, attached and made the default
    before the confirmation email is attempted, so a mail failure answers 500
    ``error.postmarkNotConfigured`` for a request that did everything asked.
    A member who retries is attaching a card they already have."""
    from profile.models import User

    profile = billing_member()
    stripe_stub.set("PaymentMethod.retrieve", a_card())

    def _no_mail(self, subject, message):
        raise RuntimeError("Postmark is not configured")

    monkeypatch.setattr(User, "email_notification", _no_mail)

    response = as_member(profile).post(
        "/api/billing/card/", {"paymentMethodId": "pm_new"}
    )

    assert response.status_code == 500
    assert response.json() == {"message": "error.postmarkNotConfigured"}
    assert fresh(profile).stripe_payment_method_id == "pm_new"
    assert stripe_stub.called("PaymentMethod.attach")
    assert stripe_stub.called("Customer.modify")
    assert len(captured) == 1


def test_removing_a_card_stripe_has_already_detached_fails_and_keeps_the_record(
    billing_member, as_member, stripe_stub
):
    """DEFECT, pinned: the local fields are cleared only if ``detach`` succeeds.

    A card removed in the Stripe dashboard (or detached by an earlier request
    whose response was lost) makes ``PaymentMethod.detach`` raise. The view
    does not catch it, so the member sees a 500 and the portal goes on showing
    a card that no longer exists, with no way to remove it.
    """
    profile = billing_member()
    stripe_stub.set(
        "PaymentMethod.detach",
        invalid_request(
            "The payment method you provided is not attached to a customer."
        ),
    )

    with pytest.raises(stripe.error.InvalidRequestError):
        as_member(profile).delete("/api/billing/card/")

    assert fresh(profile).stripe_card_last_digits == "4242"


def test_removing_a_card_when_none_is_saved_asks_stripe_nothing(
    make_member, as_member, stripe_stub, set_config
):
    set_config(ENABLE_STRIPE=True)
    profile = make_member(state="noob", rfid="BILL-NOCARD")

    response = as_member(profile).delete("/api/billing/card/")

    assert response.status_code == 200
    assert not stripe_stub.called("PaymentMethod.detach")


# --------------------------------------------------------------------------
# POST /api/billing/plans/<id>/signup/
# --------------------------------------------------------------------------


def test_signing_up_to_a_plan_that_does_not_exist_is_a_500(
    billing_member, as_member, stripe_stub
):
    """DEFECT, pinned: ``PaymentPlan.objects.get(pk=plan_id)`` is unchecked.

    A plan id from a stale page, or a mistyped one, raises ``DoesNotExist``
    where a 404 is the answer.
    """
    from api_admin_tools.models import PaymentPlan

    profile = billing_member()

    with pytest.raises(PaymentPlan.DoesNotExist):
        as_member(profile).post("/api/billing/plans/9999/signup/")

    assert not stripe_stub.called("Subscription.create")


def test_a_plan_the_tiers_page_hides_can_still_be_bought_by_id(
    billing_member, as_member, make_tier_and_plan, stripe_stub
):
    """Pinned as behaviour: ``visible`` filters the listing, not the purchase.

    ``/api/billing/tiers/`` offers only visible tiers and plans; the signup
    endpoint takes any plan id. Hiding a plan therefore stops it being
    advertised but not bought, which matters if hidden plans are meant for
    particular members (a concession rate, an old price).
    """
    profile = billing_member()
    _tier, hidden = make_tier_and_plan(tier_name="Concession", tier_stripe_id="prod_c")
    hidden.visible = False
    hidden.save()
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_hidden", "status": "active"}),
    )

    response = as_member(profile).post(f"/api/billing/plans/{hidden.id}/signup/")

    assert response.json() == {"success": True}
    assert fresh(profile).membership_plan_id == hidden.id


def test_a_test_key_used_against_live_objects_is_reported_with_stripes_message(
    billing_member, as_member, make_tier_and_plan, stripe_stub
):
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    message = "No such price: 'price_x'; a similar object exists in live mode"
    stripe_stub.set("Subscription.create", invalid_request(message))

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.status_code == 500
    assert response.json() == {"success": False, "message": message}
    assert "used test key with production object" in event_log(profile)[-1].description


def test_any_other_invalid_request_is_a_500_with_no_message(
    billing_member, as_member, make_tier_and_plan, stripe_stub, captured
):
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create",
        invalid_request("Invalid integer: abc", code="parameter_invalid_integer"),
    )

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.status_code == 500
    assert response.json() == {"success": False, "message": None}
    assert event_log(profile)[-1].description == (
        "InvalidRequestError from Stripe while creating subscription."
    )
    assert len(captured) == 1


def test_an_invalid_request_with_no_error_code_is_an_unhandled_error(
    billing_member, as_member, make_tier_and_plan, stripe_stub
):
    """DEFECT, pinned: ``error["code"]`` is indexed, not ``.get``.

    Stripe omits ``code`` from some invalid-request errors. The handler that is
    meant to turn them into a tidy 500 raises ``KeyError`` instead, from inside
    the ``except`` block, so the sibling ``except Exception`` does not catch it.
    """
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create", invalid_request("Something is wrong.", code=None)
    )

    with pytest.raises(KeyError):
        as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")


def test_an_invalid_request_with_no_json_body_is_an_unhandled_error(
    billing_member, as_member, make_tier_and_plan, stripe_stub
):
    """DEFECT, pinned: ``e.json_body.get(...)`` assumes there is a body."""
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create", invalid_request("Gateway timeout", body=False)
    )

    with pytest.raises(AttributeError):
        as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")


def test_a_non_stripe_error_creating_a_subscription_is_a_500_logged_under_the_wrong_name(
    billing_member, as_member, make_tier_and_plan, stripe_stub, captured
):
    """The catch-all handler reuses the ``InvalidRequestError`` log text."""
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set("Subscription.create", stripe.error.APIConnectionError("no route"))

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.status_code == 500
    assert response.json() == {"success": False, "message": None}
    assert event_log(profile)[-1].description == (
        "InvalidRequestError from Stripe while creating subscription."
    )
    assert len(captured) == 1


@pytest.mark.parametrize(
    "stripe_status", ["incomplete", "trialing", "past_due", "unpaid", "canceled"]
)
def test_a_subscription_that_is_not_active_is_created_in_stripe_but_never_recorded(
    billing_member, as_member, make_tier_and_plan, stripe_stub, stripe_status
):
    """DEFECT, pinned: the Stripe subscription is orphaned, and a retry doubles it.

    Only ``"active"`` records anything. For ``"incomplete"`` the member is told
    ``signup.subscriptionFailed`` (with ``success: true``); for every other
    status they are simply told ``success: true``. In both cases the
    subscription now exists in Stripe -- an incomplete one waits for the
    payment to be confirmed, a trialing one will start charging -- and the
    profile has no plan and no subscription id. The member is still free to
    sign up, so trying again creates a second subscription for the same plan.
    """
    profile = billing_member()
    _tier, plan = make_tier_and_plan()
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_orphan", "status": stripe_status}),
    )

    first = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")
    second = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert first.json()["success"] is True and second.json()["success"] is True
    saved = fresh(profile)
    assert (saved.membership_plan, saved.stripe_subscription_id) == (None, "")
    assert saved.subscription_status == "inactive"
    assert len(stripe_stub.calls_to("Subscription.create")) == 2


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


def test_a_subscription_stripe_no_longer_has_makes_the_account_page_a_500(
    billing_member, as_member, stripe_stub
):
    """DEFECT, pinned: ``Subscription.retrieve`` is bare.

    The local ``stripe_subscription_id`` is only cleared by the
    ``customer.subscription.deleted`` webhook. If that is missed, or the
    subscription is deleted in the Stripe dashboard first, every visit to the
    page that calls this endpoint raises.
    """
    profile = billing_member(with_plan=True, with_subscription=True)
    stripe_stub.set(
        "Subscription.retrieve",
        invalid_request("No such subscription: 'sub_fail'", http_status=404),
    )

    with pytest.raises(stripe.error.InvalidRequestError):
        as_member(profile).get("/api/billing/myplan/")


# --------------------------------------------------------------------------
# POST /api/billing/myplan/<resume|cancel>/
# --------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["cancel", "pause", "resumed", "Resume", "x"])
def test_any_word_but_exactly_resume_cancels_the_plan(
    billing_member, as_member, stripe_stub, sent_emails, word
):
    """DEFECT, pinned: the URL is ``myplan/<str:resume>/`` and only "resume" is special.

    ``resume = True if resume == "resume" else False``. The frontend sends
    "cancel" or "resume", but a typo, a different capitalisation, or a request
    made by hand to ``/api/billing/myplan/anything/`` schedules the
    cancellation of the member's subscription in Stripe.
    """
    profile = billing_member(with_plan=True, with_subscription=True)
    stripe_stub.set(
        "Subscription.modify", FakeStripeObject({"cancel_at_period_end": True})
    )

    response = as_member(profile).post(f"/api/billing/myplan/{word}/")

    assert response.json() == {"success": True}
    assert stripe_stub.calls_to("Subscription.modify")[0][2] == {
        "cancel_at_period_end": True
    }
    assert fresh(profile).subscription_status == "cancelling"


def test_cancelling_a_plan_with_no_stripe_subscription_asks_stripe_to_modify_a_blank_id(
    billing_member, as_member, stripe_stub
):
    """DEFECT, pinned: only the *resume* branch checks ``stripe_subscription_id``.

    An administrator can give a member a plan by hand, and a subscription that
    was never recorded (see above) leaves the same state. Cancelling then
    calls ``Subscription.modify("", ...)`` (the column defaults to the empty
    string, not null), which Stripe rejects, and the
    member sees a 500 for a plan they were never being billed for.
    """
    profile = billing_member(with_plan=True, with_subscription=False)
    stripe_stub.set("Subscription.modify", invalid_request("Invalid subscription id: "))

    with pytest.raises(stripe.error.InvalidRequestError):
        as_member(profile).post("/api/billing/myplan/cancel/")

    assert stripe_stub.calls_to("Subscription.modify")[0][1] == ("",)


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


@pytest.mark.parametrize("stripe_status", ["incomplete", "trialing", "past_due"])
def test_restarting_a_lapsed_plan_has_the_same_orphaned_subscription_problem(
    billing_member, as_member, stripe_stub, stripe_status
):
    """DEFECT, pinned: the signup defect, repeated in the resume branch.

    Only ``"active"`` records the new subscription id. For anything else the
    member is told ``success: true`` -- with ``signup.subscriptionFailed`` for
    ``"incomplete"`` -- while the subscription exists in Stripe and the profile
    has no id for it. Resuming again creates another.
    """
    profile = billing_member(with_plan=True, with_subscription=False)
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_orphan", "status": stripe_status}),
    )

    first = as_member(profile).post("/api/billing/myplan/resume/")
    second = as_member(profile).post("/api/billing/myplan/resume/")

    expected = {"success": True}
    if stripe_status == "incomplete":
        expected["message"] = "signup.subscriptionFailed"
    assert first.json() == second.json() == expected
    assert fresh(profile).stripe_subscription_id == ""
    assert len(stripe_stub.calls_to("Subscription.create")) == 2


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


def test_a_moodle_course_with_no_percentage_leaks_a_python_error_to_the_member(
    make_member, as_member, set_config, monkeypatch, captured
):
    """DEFECT, pinned: the catch-all returns ``str(e)`` in the response.

    With a minimum score of 0, a Moodle answer whose ``percentage_completed``
    is ``None`` reaches ``None >= 0`` and raises ``TypeError``. The handler
    reports it to Sentry, which is right, and also puts the interpreter's
    message in the JSON the member's browser receives.
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

    body = response.json()
    assert (body["success"], body["score"]) == (False, 0)
    assert "'>=' not supported" in body["error"]
    assert len(captured) == 1


# --------------------------------------------------------------------------
# POST /api/billing/access-card/
# --------------------------------------------------------------------------


def test_assigning_a_card_without_the_field_is_an_unhandled_error(
    make_member, as_member
):
    """DEFECT, pinned: ``request.data["accessCard"]`` is indexed, not ``.get``."""
    profile = make_member(state="noob", rfid="BILL-CARD")

    with pytest.raises(KeyError):
        as_member(profile).post("/api/billing/access-card/", {}, format="json")


def test_a_blank_card_is_stored_as_an_empty_string_and_the_second_one_collides(
    make_member, as_member
):
    """DEFECT, pinned: ``""`` skips the in-use check, and ``rfid`` is unique.

    ``rfid`` is ``unique=True, null=True``: any number of members can have no
    card (null), but not two with an empty string. The clash check is
    ``if access_card and clash.exists()``, which a blank value never enters, so
    the first member to submit a blank card is stored with ``""`` -- wiping any
    card they had -- and the second gets an ``IntegrityError`` and a 500.
    """
    from django.db import IntegrityError

    first = make_member(state="noob", rfid="BILL-HELD")
    second = make_member(state="noob", rfid=None)

    response = as_member(first).post(
        "/api/billing/access-card/", {"accessCard": ""}, format="json"
    )

    assert response.json() == {"success": True}
    assert fresh(first).rfid == ""

    with pytest.raises(IntegrityError):
        as_member(second).post(
            "/api/billing/access-card/", {"accessCard": ""}, format="json"
        )
