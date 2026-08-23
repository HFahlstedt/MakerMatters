"""Characterisation: card storage, tier listing and subscription management.

Every endpoint here talks to Stripe synchronously on the request path — there
is no caching and no queue — so the ``stripe_stub`` fixture stands in for the
SDK and records what would have been sent.
"""

import pytest
import stripe

from tests.conftest import FakeStripeObject

pytestmark = pytest.mark.django_db


@pytest.fixture
def subscriber(make_member, make_tier_and_plan, set_config):
    """A member with a Stripe customer, a saved card and an active plan."""

    def _build(with_plan=True, with_subscription=True, **overrides):
        set_config(ENABLE_STRIPE=True, **overrides)
        profile = make_member(state="active", rfid="TAG-BILL")
        profile.stripe_customer_id = "cus_test_bill"
        profile.stripe_payment_method_id = "pm_test_bill"
        if with_subscription:
            profile.stripe_subscription_id = "sub_test_bill"
            profile.subscription_status = "active"
        if with_plan:
            _tier, plan = make_tier_and_plan()
            profile.membership_plan = plan
        profile.save()
        return profile

    return _build


# --------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------


def test_existing_customer_gets_a_setup_intent(subscriber, as_member, stripe_stub):
    profile = subscriber()
    stripe_stub.set("Customer.retrieve", FakeStripeObject({"id": "cus_test_bill"}))
    stripe_stub.set(
        "SetupIntent.create", FakeStripeObject({"client_secret": "seti_secret"})
    )

    response = as_member(profile).get("/api/billing/card/")

    assert response.json() == {"clientSecret": "seti_secret"}
    assert not stripe_stub.called("Customer.create")


def test_a_member_without_a_customer_gets_one_created(
    make_member, as_member, stripe_stub, set_config
):
    from profile.models import Profile

    set_config(ENABLE_STRIPE=True)
    profile = make_member(state="noob", rfid="TAG-NEWCUST")
    stripe_stub.set("Customer.create", FakeStripeObject({"id": "cus_freshly_made"}))
    stripe_stub.set(
        "SetupIntent.create", FakeStripeObject({"client_secret": "seti_new"})
    )

    response = as_member(profile).get("/api/billing/card/")

    assert response.json() == {"clientSecret": "seti_new"}
    assert Profile.objects.get(pk=profile.pk).stripe_customer_id == "cus_freshly_made"


def test_a_deleted_stripe_customer_is_replaced(subscriber, as_member, stripe_stub):
    """Stripe keeps deleted customers retrievable; the view must notice the flag."""
    from profile.models import Profile

    profile = subscriber()
    stripe_stub.set("Customer.retrieve", FakeStripeObject({"deleted": True}))
    stripe_stub.set("Customer.create", FakeStripeObject({"id": "cus_replacement"}))
    stripe_stub.set("SetupIntent.create", FakeStripeObject({"client_secret": "seti_x"}))

    as_member(profile).get("/api/billing/card/")

    assert Profile.objects.get(pk=profile.pk).stripe_customer_id == "cus_replacement"


def test_saving_a_card_records_only_the_last_four_and_expiry(
    subscriber, as_member, stripe_stub, sent_emails
):
    """Card details never touch the database — only a display fragment is kept."""
    from profile.models import Profile

    profile = subscriber()
    stripe_stub.set(
        "PaymentMethod.retrieve",
        FakeStripeObject({"card": {"last4": "4242", "exp_month": 7, "exp_year": 2030}}),
    )

    response = as_member(profile).post(
        "/api/billing/card/", {"paymentMethodId": "pm_new_card"}, format="json"
    )

    assert response.status_code == 200
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.stripe_card_last_digits == "4242"
    assert refreshed.stripe_card_expiry == "07/2030"
    assert refreshed.stripe_payment_method_id == "pm_new_card"

    assert stripe_stub.called("PaymentMethod.attach")
    assert stripe_stub.called("Customer.modify")
    assert len(sent_emails) == 1


def test_removing_a_card_detaches_it_and_clears_the_fragment(
    subscriber, as_member, stripe_stub
):
    from profile.models import Profile

    profile = subscriber()
    profile.stripe_card_last_digits = "4242"
    profile.save()

    response = as_member(profile).delete("/api/billing/card/")

    assert response.status_code == 200
    assert stripe_stub.called("PaymentMethod.detach")
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.stripe_payment_method_id == ""
    assert refreshed.stripe_card_last_digits == ""


# --------------------------------------------------------------------------
# Tier listing
# --------------------------------------------------------------------------


def test_only_visible_tiers_and_plans_are_listed(
    make_member, as_member, make_tier_and_plan, set_config
):
    from api_admin_tools.models import MemberTier, PaymentPlan

    set_config(ENABLE_STRIPE=True)
    _tier, visible_plan = make_tier_and_plan()

    hidden_tier = MemberTier.objects.create(
        name="Secret", description="Hidden tier", stripe_id="prod_hidden", visible=False
    )
    PaymentPlan.objects.create(
        name="Hidden plan",
        stripe_id="price_hidden",
        member_tier=hidden_tier,
        visible=True,
        currency="aud",
        cost=100,
        interval_count=1,
        interval="month",
    )

    profile = make_member()
    response = as_member(profile).get("/api/billing/tiers/")

    body = response.json()
    assert [t["name"] for t in body] == ["Full Member"]
    assert body[0]["plans"] == [
        {
            "id": visible_plan.id,
            "name": "Monthly",
            "currency": "aud",
            "cost": 2500,
            "intervalAmount": 1,
            "interval": "month",
        }
    ]


# --------------------------------------------------------------------------
# Signing up to a plan
# --------------------------------------------------------------------------


def test_signing_up_to_a_plan_records_the_subscription(
    make_member, as_member, make_tier_and_plan, stripe_stub, set_config
):
    from profile.models import Profile

    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan()
    profile = make_member(state="noob", rfid="TAG-PLAN")
    profile.stripe_customer_id = "cus_plan"
    profile.save()

    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_created", "status": "active"}),
    )

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.json() == {"success": True}
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.stripe_subscription_id == "sub_created"
    assert refreshed.membership_plan_id == plan.id
    assert refreshed.subscription_status == "active"


def test_an_incomplete_subscription_is_reported_to_the_member(
    make_member, as_member, make_tier_and_plan, stripe_stub, set_config
):
    """Stripe returns 'incomplete' when the first payment needs action (e.g. 3DS)."""
    from profile.models import Profile

    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan()
    profile = make_member(state="noob", rfid="TAG-PLAN")
    profile.stripe_customer_id = "cus_plan"
    profile.save()

    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_incomplete", "status": "incomplete"}),
    )

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.json() == {"success": True, "message": "signup.subscriptionFailed"}
    # Nothing is recorded when the subscription did not become active.
    assert Profile.objects.get(pk=profile.pk).membership_plan is None


def test_signing_up_twice_is_refused(subscriber, as_member, stripe_stub):
    profile = subscriber()

    response = as_member(profile).post(
        f"/api/billing/plans/{profile.membership_plan_id}/signup/"
    )

    assert response.status_code == 409
    assert not stripe_stub.called("Subscription.create")


def test_missing_default_payment_method_retry_is_broken(
    make_member, as_member, make_tier_and_plan, stripe_stub, set_config
):
    """DEFECT, pinned: the retry passes the wrong argument.

    ``PaymentPlanSignup.create_subscription(self, request, new_plan, attempts=0)``
    recovers from a missing default payment method by setting one and retrying::

        return self.create_subscription(attempts)

    ``attempts`` (an int) lands in the ``request`` slot and ``new_plan`` is not
    supplied at all, so the recovery path raises ``TypeError`` instead of
    retrying. The member sees a 500 rather than a completed signup.
    """
    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan()
    profile = make_member(state="noob", rfid="TAG-RETRY")
    profile.stripe_customer_id = "cus_retry"
    profile.save()

    stripe_stub.set(
        "Subscription.create",
        stripe.error.InvalidRequestError(
            "This customer has no attached default payment method.",
            param=None,
            json_body={
                "error": {
                    "code": "resource_missing",
                    "message": "This customer has no attached default payment method.",
                }
            },
        ),
    )

    with pytest.raises(TypeError, match="new_plan"):
        as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    # It did attempt the recovery before falling over.
    assert stripe_stub.called("Customer.modify")


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: create_subscription() retries with self.create_subscription(attempts), "
    "passing the attempt count where `request` belongs and omitting `new_plan`.",
)
def test_missing_default_payment_method_should_be_recovered(
    make_member, as_member, make_tier_and_plan, stripe_stub, set_config
):
    """The behaviour we WANT: set the default payment method, retry, succeed."""
    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan()
    profile = make_member(state="noob", rfid="TAG-RETRY-OK")
    profile.stripe_customer_id = "cus_retry_ok"
    profile.save()

    attempts = {"n": 0}

    def _create(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise stripe.error.InvalidRequestError(
                "This customer has no attached default payment method.",
                param=None,
                json_body={
                    "error": {
                        "code": "resource_missing",
                        "message": "This customer has no attached default payment method.",
                    }
                },
            )
        return FakeStripeObject({"id": "sub_retried", "status": "active"})

    stripe_stub.set("Subscription.create", _create)

    response = as_member(profile).post(f"/api/billing/plans/{plan.id}/signup/")

    assert response.json() == {"success": True}


# --------------------------------------------------------------------------
# Viewing, cancelling and resuming
# --------------------------------------------------------------------------


def test_subscription_details_are_fetched_live_from_stripe(
    subscriber, as_member, stripe_stub
):
    profile = subscriber()
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

    response = as_member(profile).get("/api/billing/myplan/")

    body = response.json()
    assert body["success"] is True
    assert body["subscription"]["currentPeriodEnd"] == 1702592000
    assert body["subscription"]["membershipPlan"]["cost"] == 2500
    assert body["subscription"]["membershipTier"]["name"] == "Full Member"


def test_a_member_without_a_plan_has_no_subscription(
    subscriber, as_member, stripe_stub
):
    profile = subscriber(with_plan=False, with_subscription=False)

    response = as_member(profile).get("/api/billing/myplan/")

    assert response.json() == {"success": False}
    assert not stripe_stub.called("Subscription.retrieve")


def test_cancelling_schedules_the_subscription_to_end(
    subscriber, as_member, stripe_stub, sent_emails
):
    """Cancellation is deferred to the period end, so access is not cut immediately."""
    from profile.models import Profile

    profile = subscriber()
    stripe_stub.set(
        "Subscription.modify", FakeStripeObject({"cancel_at_period_end": True})
    )

    response = as_member(profile).post("/api/billing/myplan/cancel/")

    assert response.json() == {"success": True}
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.subscription_status == "cancelling"
    assert refreshed.state == "active"  # access continues until the period ends

    _path, _args, kwargs = stripe_stub.calls_to("Subscription.modify")[0]
    assert kwargs["cancel_at_period_end"] is True
    assert len(sent_emails) == 2  # committee + member


def test_resuming_clears_the_scheduled_cancellation(
    subscriber, as_member, stripe_stub, sent_emails
):
    from profile.models import Profile

    profile = subscriber()
    profile.subscription_status = "cancelling"
    profile.save()
    stripe_stub.set(
        "Subscription.modify", FakeStripeObject({"cancel_at_period_end": False})
    )

    response = as_member(profile).post("/api/billing/myplan/resume/")

    assert response.json() == {"success": True}
    assert Profile.objects.get(pk=profile.pk).subscription_status == "active"

    _path, _args, kwargs = stripe_stub.calls_to("Subscription.modify")[0]
    assert kwargs["cancel_at_period_end"] is False


def test_resuming_without_a_stripe_subscription_creates_a_new_one(
    subscriber, as_member, stripe_stub, sent_emails
):
    """A member whose subscription fully lapsed can restart it from their existing plan."""
    from profile.models import Profile

    profile = subscriber(with_subscription=False)
    stripe_stub.set(
        "Subscription.create",
        FakeStripeObject({"id": "sub_restarted", "status": "active"}),
    )

    response = as_member(profile).post("/api/billing/myplan/resume/")

    assert response.json() == {"success": True}
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.stripe_subscription_id == "sub_restarted"
    assert refreshed.subscription_status == "active"


def test_modifying_a_nonexistent_plan_is_refused(
    make_member, as_member, stripe_stub, set_config
):
    set_config(ENABLE_STRIPE=True)
    profile = make_member(state="noob")

    response = as_member(profile).post("/api/billing/myplan/cancel/")

    assert response.status_code == 404
    assert response.json()["message"] == "paymentPlan.notExists"
