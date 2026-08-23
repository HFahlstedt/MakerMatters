"""Characterisation: the Stripe webhook.

This endpoint is the *second*, asynchronous route into an active membership.
A member can complete signup themselves, or an ``invoice.paid`` event can
activate them independently. Both write the same fields, and the webhook is
what reconciles ``Profile.state`` with ``Profile.subscription_status``.

It is unauthenticated by design (Stripe calls it), so signature verification is
the only thing standing between the public internet and membership activation —
and it only happens when ``STRIPE_WEBHOOK_SECRET`` is set.
"""

import pytest

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/api/billing/stripe-webhook/"
CUSTOMER_ID = "cus_test_12345"


def event(event_type, **object_fields):
    """Build a Stripe-shaped event body."""
    return {
        "type": event_type,
        "data": {"object": {"customer": CUSTOMER_ID, **object_fields}},
    }


@pytest.fixture
def stripe_member(make_member, set_config):
    """A member linked to a Stripe customer, with webhook signing disabled."""

    def _build(state="noob", rfid="TAG-STRIPE", **overrides):
        set_config(STRIPE_WEBHOOK_SECRET="", MAX_INDUCTION_DAYS=0, **overrides)
        profile = make_member(state=state, rfid=rfid)
        profile.stripe_customer_id = CUSTOMER_ID
        profile.stripe_subscription_id = "sub_test_12345"
        profile.save()
        return profile

    return _build


# --------------------------------------------------------------------------
# invoice.paid
# --------------------------------------------------------------------------


def test_paid_invoice_activates_a_member_who_meets_every_requirement(
    stripe_member, api_client, sent_emails
):
    from profile.models import Profile

    profile = stripe_member(state="noob", rfid="TAG-STRIPE")

    response = api_client.post(
        WEBHOOK_URL, event("invoice.paid", status="paid"), format="json"
    )

    assert response.status_code == 200
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.state == "active"
    assert refreshed.subscription_status == "active"


def test_paid_invoice_stamps_the_first_subscription_date_once(
    stripe_member, api_client, sent_emails
):
    from profile.models import Profile

    profile = stripe_member(state="noob")

    api_client.post(WEBHOOK_URL, event("invoice.paid", status="paid"), format="json")
    first_stamp = Profile.objects.get(pk=profile.pk).subscription_first_created
    assert first_stamp is not None

    api_client.post(WEBHOOK_URL, event("invoice.paid", status="paid"), format="json")
    assert Profile.objects.get(pk=profile.pk).subscription_first_created == first_stamp


def test_paid_invoice_marks_the_subscription_active_without_activating_an_ineligible_member(
    stripe_member, api_client, sent_emails
):
    """Payment alone does not grant site access — the requirements still gate it."""
    from profile.models import Profile

    profile = stripe_member(state="noob", rfid=None)

    response = api_client.post(
        WEBHOOK_URL, event("invoice.paid", status="paid"), format="json"
    )

    assert response.status_code == 200
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.state == "noob"
    assert refreshed.subscription_status == "active"
    assert any("payment was successful" in e["Subject"] for e in sent_emails)


def test_an_unpaid_invoice_changes_nothing(stripe_member, api_client, sent_emails):
    from profile.models import Profile

    profile = stripe_member(state="noob")

    api_client.post(WEBHOOK_URL, event("invoice.paid", status="open"), format="json")

    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.state == "noob"
    assert refreshed.subscription_status == "inactive"


def test_an_already_active_member_is_left_alone(stripe_member, api_client, sent_emails):
    from profile.models import Profile

    profile = stripe_member(state="active")

    api_client.post(WEBHOOK_URL, event("invoice.paid", status="paid"), format="json")

    assert Profile.objects.get(pk=profile.pk).state == "active"


def test_returning_ineligible_member_crashes_the_webhook(
    stripe_member, api_client, sent_emails
):
    """DEFECT, pinned: ``send_email_to_admin`` is called with positional arguments.

    In the "returning member paid but is not eligible" branch::

        send_email_to_admin(subject, title, message, reply_to=...)

    but the signature is ``(subject, template_vars, template_name=None, ...)``.
    So ``template_vars`` receives a plain string, and ``send_single_email``
    immediately calls ``template_vars.get("message")`` on it.

    Effect: any *returning* member (state ``inactive`` or ``accountonly``) who
    pays an invoice without meeting the requirements triggers a 500. Stripe
    then retries the webhook, re-sending the member's confirmation email each
    time. New members are unaffected, because the branch is skipped for ``noob``.
    """
    profile = stripe_member(state="inactive", rfid=None)

    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        api_client.post(
            WEBHOOK_URL, event("invoice.paid", status="paid"), format="json"
        )

    # The member email went out before the crash, so a retry duplicates it.
    assert any("payment was successful" in e["Subject"] for e in sent_emails)


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: send_email_to_admin() is called positionally in the returning-member "
    "branch of invoice.paid, passing a string where template_vars is expected.",
)
def test_returning_ineligible_member_should_notify_the_committee(
    stripe_member, api_client, sent_emails
):
    """The behaviour we WANT: notify the committee, return 200, do not crash."""
    stripe_member(state="inactive", rfid=None)

    response = api_client.post(
        WEBHOOK_URL, event("invoice.paid", status="paid"), format="json"
    )

    assert response.status_code == 200
    assert any("Verify returning member" in e["Subject"] for e in sent_emails)


# --------------------------------------------------------------------------
# invoice.payment_failed
# --------------------------------------------------------------------------


def test_failed_payment_emails_the_member_without_changing_state(
    stripe_member, api_client, sent_emails
):
    """Stripe's own retry schedule governs; the server only warns the member."""
    from profile.models import Profile

    profile = stripe_member(state="active")

    response = api_client.post(
        WEBHOOK_URL, event("invoice.payment_failed"), format="json"
    )

    assert response.status_code == 200
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.state == "active"
    assert refreshed.subscription_status == "inactive"  # untouched by this branch

    assert len(sent_emails) == 1
    assert sent_emails[0]["Subject"] == "Your membership payment failed"


# --------------------------------------------------------------------------
# customer.subscription.deleted
# --------------------------------------------------------------------------


def test_deleted_subscription_deactivates_the_member(
    stripe_member, api_client, sent_emails
):
    from profile.models import Profile

    profile = stripe_member(state="active")

    response = api_client.post(
        WEBHOOK_URL, event("customer.subscription.deleted"), format="json"
    )

    assert response.status_code == 200
    refreshed = Profile.objects.get(pk=profile.pk)
    assert refreshed.state == "inactive"
    assert refreshed.subscription_status == "inactive"
    assert refreshed.membership_plan is None
    assert refreshed.stripe_subscription_id is None


def test_deleted_subscription_revokes_device_access(
    stripe_member, api_client, make_door, sent_emails
):
    """Deactivation must drop the member's tag from every door's cached list."""
    door = make_door(serial="door-cancel")
    profile = stripe_member(state="active")
    profile.doors.add(door)

    api_client.post(WEBHOOK_URL, event("customer.subscription.deleted"), format="json")

    # The permission link survives; it is the *state* that removes them from the sync.
    tags, _hash = door.get_tags()
    assert tags == []


def test_deleted_subscription_emails_member_and_committee(
    stripe_member, api_client, sent_emails
):
    stripe_member(state="active")

    api_client.post(WEBHOOK_URL, event("customer.subscription.deleted"), format="json")

    subjects = [e["Subject"] for e in sent_emails]
    assert any("disabled" in s for s in subjects)  # from deactivate()
    assert "Your membership has been cancelled" in subjects
    assert any("was just cancelled" in s for s in subjects)


# --------------------------------------------------------------------------
# Customer resolution and signature verification
# --------------------------------------------------------------------------


def test_events_for_unknown_customers_are_silently_accepted(
    make_member, api_client, set_config
):
    """A shared Stripe account may process payments unrelated to this system."""
    set_config(STRIPE_WEBHOOK_SECRET="")
    make_member(state="active")

    response = api_client.post(
        WEBHOOK_URL,
        {
            "type": "invoice.paid",
            "data": {"object": {"customer": "cus_someone_else", "status": "paid"}},
        },
        format="json",
    )

    assert response.status_code == 200


def test_ambiguous_customer_lookup_is_unguarded(make_member, api_client, set_config):
    """DEFECT, pinned: only ``DoesNotExist`` is caught, not ``MultipleObjectsReturned``.

    ``stripe_customer_id`` defaults to the empty string and is not unique, so
    every member who has never saved a card shares that value. An event whose
    customer field is empty therefore raises rather than being ignored.
    """
    from profile.models import Profile

    set_config(STRIPE_WEBHOOK_SECRET="")
    make_member(state="active", rfid="TAG-A")
    make_member(state="active", rfid="TAG-B")

    with pytest.raises(Profile.MultipleObjectsReturned):
        api_client.post(
            WEBHOOK_URL,
            {
                "type": "invoice.paid",
                "data": {"object": {"customer": "", "status": "paid"}},
            },
            format="json",
        )


def test_a_configured_secret_makes_the_signature_authoritative(
    stripe_member, api_client, set_config, stripe_stub, sent_emails
):
    """With a secret set, the request body is ignored in favour of the verified event."""
    from tests.conftest import FakeStripeObject

    profile = stripe_member(state="noob")
    set_config(STRIPE_WEBHOOK_SECRET="whsec_test")

    stripe_stub.set(
        "Webhook.construct_event",
        FakeStripeObject(
            {
                "type": "invoice.paid",
                "data": {"object": {"customer": CUSTOMER_ID, "status": "paid"}},
            }
        ),
    )

    response = api_client.post(
        WEBHOOK_URL,
        {"type": "customer.subscription.deleted", "data": {"object": {}}},
        format="json",
        HTTP_STRIPE_SIGNATURE="t=1,v1=deadbeef",
    )

    assert response.status_code == 200
    assert stripe_stub.called("Webhook.construct_event")

    from profile.models import Profile

    # The verified event (invoice.paid) won, not the unsigned body.
    assert Profile.objects.get(pk=profile.pk).state == "active"


def test_an_invalid_signature_is_rejected_without_side_effects(
    stripe_member, api_client, set_config, stripe_stub
):
    from profile.models import Profile

    profile = stripe_member(state="noob")
    set_config(STRIPE_WEBHOOK_SECRET="whsec_test")
    stripe_stub.set("Webhook.construct_event", ValueError("bad signature"))

    response = api_client.post(
        WEBHOOK_URL,
        event("invoice.paid", status="paid"),
        format="json",
        HTTP_STRIPE_SIGNATURE="t=1,v1=forged",
    )

    assert response.json() == {"error": "Error validating Stripe signature."}
    assert Profile.objects.get(pk=profile.pk).state == "noob"


def test_the_webhook_requires_no_authentication(stripe_member, api_client, sent_emails):
    """Stripe cannot authenticate; confirm the endpoint stays open."""
    stripe_member(state="noob")

    response = api_client.post(
        WEBHOOK_URL, event("invoice.paid", status="paid"), format="json"
    )

    assert response.status_code == 200
