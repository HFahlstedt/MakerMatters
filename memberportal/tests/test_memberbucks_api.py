"""Characterisation: the member wallet under ``/api/memberbucks/``.

Memberbucks is the portal's built-in currency: members top it up with a
saved card and spend it at vending machines, on interlock sessions, or
through the portal itself. The device side is covered by
``test_device_protocol_memberbucks.py``; this file covers the five endpoints
a member or an admin calls directly.

Two things about the model shape everything below.

The balance on ``Profile`` is not a running total that endpoints adjust. It
is re-derived on every ``MemberBucks.save()`` as the sum of the member's
whole ledger, so it cannot drift from the transactions that make it up. What
that does *not* give is any protection between reading the balance and
writing a debit -- see ``test_two_simultaneous_payments_can_overdraw_a_wallet``.

And the two endpoints that move money take their amount in different units
behind identical-looking URLs: ``add/<amount>/`` is whole dollars,
``pay/<amount>/`` is cents. Neither is wrong on its own, but a client that
reuses one call shape for the other is off by a factor of a hundred.
"""

from datetime import timedelta

import pytest
import stripe
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.conftest import FakeStripeObject

pytestmark = pytest.mark.django_db


TRANSACTION_FIELDS = {"amount", "type", "description", "date"}
BALANCE_LIST_ROW_FIELDS = {
    "first_name",
    "last_name",
    "screen_name",
    "memberbucks_balance",
}


def credit(profile, amount, transaction_type="cash", description="Seed"):
    """Add a ledger entry the ordinary way, so the balance is re-derived."""
    from memberbucks.models import MemberBucks

    entry = MemberBucks.objects.create(
        user=profile.user,
        amount=amount,
        transaction_type=transaction_type,
        description=description,
    )
    profile.refresh_from_db()
    return entry


def fresh_client(profile):
    """A client authenticated with its own freshly loaded ``User``.

    ``as_member`` hands every request the same Python object, whose cached
    ``profile`` would hide a balance changed by another request. Real requests
    each load the user from the database, and the concurrency test needs that.
    """
    from profile.models import User
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=User.objects.get(pk=profile.user.pk))
    return client


def card_declined(code="card_declined", payment_intent_id="pi_declined"):
    """A ``CardError`` shaped the way Stripe actually returns one.

    The payment intent rides along inside the error body: a request that
    creates and confirms an intent in one call fails *with* that intent.
    """
    body = {
        "error": {
            "type": "card_error",
            "code": code,
            "message": "Your card was declined.",
            "payment_intent": {"id": payment_intent_id, "object": "payment_intent"},
        }
    }
    return stripe.error.CardError("Your card was declined.", None, code, json_body=body)


@pytest.fixture
def member_with_card(make_member):
    return make_member(
        state="active",
        rfid="WALLET-CARD",
        stripe_customer_id="cus_test",
        stripe_payment_method_id="pm_test",
    )


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/memberbucks/transactions/"),
        ("get", "/api/memberbucks/balance/"),
        ("post", "/api/memberbucks/add/10/"),
        ("post", "/api/memberbucks/pay/100/"),
        ("get", "/api/memberbucks/balance-list/"),
    ],
)
def test_an_anonymous_caller_is_refused(api_client, method, path):
    assert getattr(api_client, method)(path).status_code == 401


def test_a_plain_member_cannot_read_everyones_balance(make_member):
    member = make_member(state="active", rfid="WALLET-LIST-1")

    assert fresh_client(member).get("/api/memberbucks/balance-list/").status_code == 403


def test_the_balance_list_admits_staff_and_the_generic_api_key(as_admin, with_api_key):
    """``HasAPIKey`` is the generic ``rest_framework_api_key.APIKey`` model.

    It is a third kind of key, separate from the device and external
    access-control keys, and the same one that admits updates to the space
    directory -- so issuing one for either purpose grants both.
    """
    assert as_admin().get("/api/memberbucks/balance-list/").status_code == 200
    assert with_api_key().get("/api/memberbucks/balance-list/").status_code == 200


def test_a_device_key_does_not_open_the_balance_list(api_client, device_api_key):
    api_client.credentials(HTTP_AUTHORIZATION=f"Api-Key {device_api_key}")

    assert api_client.get("/api/memberbucks/balance-list/").status_code == 401


# --------------------------------------------------------------------------
# /api/memberbucks/balance/ and /transactions/
# --------------------------------------------------------------------------


def test_the_balance_is_the_sum_of_the_ledger(make_member):
    member = make_member(state="active", rfid="WALLET-BAL")
    credit(member, 12.5)
    credit(member, -2.25)

    body = fresh_client(member).get("/api/memberbucks/balance/").data

    assert body == {"balance": 10.25}


def test_the_transaction_list_shows_only_the_callers_own_entries(make_member):
    member = make_member(state="active", rfid="WALLET-TX-1")
    other = make_member(state="active", rfid="WALLET-TX-2")
    credit(member, 5, description="Mine")
    credit(other, 7, description="Theirs")

    body = fresh_client(member).get("/api/memberbucks/transactions/").json()

    assert [entry["description"] for entry in body] == ["Mine"]
    assert set(body[0]) == TRANSACTION_FIELDS
    # The display half of the choices tuple, not the stored "cash".
    assert body[0]["type"] == "Cash"


def test_the_transaction_list_is_newest_first_and_capped_at_100(make_member):
    from memberbucks.models import MemberBucks

    member = make_member(state="active", rfid="WALLET-TX-3")
    start = timezone.now() - timedelta(days=1)
    for i in range(101):
        entry = credit(member, 1, description=f"#{i}")
        # ``auto_now_add`` ignores a date passed to create(), so set it after.
        MemberBucks.objects.filter(pk=entry.pk).update(
            date=start + timedelta(minutes=i)
        )

    body = fresh_client(member).get("/api/memberbucks/transactions/").json()

    assert len(body) == 100
    assert body[0]["description"] == "#100"
    # The oldest of the 101 is the one dropped.
    assert body[-1]["description"] == "#1"


def test_the_transaction_list_loads_the_members_whole_ledger(make_member):
    """DEFECT, pinned: the 100 cap is applied in Python, not in SQL.

    ``order_by("date")[::-1][:100]`` -- the same pattern that loaded the whole
    access log in ``SwipesList``. Django will not push a negative slice step
    into a query, so every transaction the member has ever made is fetched,
    reversed in Python, and only then cut to 100. Scoped to one member, so the
    cost grows with a member's history rather than the whole space's.
    """
    member = make_member(state="active", rfid="WALLET-TX-4")
    credit(member, 1)

    with CaptureQueriesContext(connection) as queries:
        fresh_client(member).get("/api/memberbucks/transactions/")

    ledger_queries = [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].lstrip().upper().startswith("SELECT")
        and "memberbucks_memberbucks" in q["sql"]
        and "SUM(" not in q["sql"].upper()
    ]
    assert len(ledger_queries) == 1
    assert "LIMIT" not in ledger_queries[0].upper()


# --------------------------------------------------------------------------
# /api/memberbucks/pay/<cents>/
# --------------------------------------------------------------------------


def test_paying_debits_the_amount_in_cents(make_member):
    from memberbucks.models import MemberBucks

    member = make_member(state="active", rfid="WALLET-PAY-1")
    credit(member, 10)

    response = fresh_client(member).post("/api/memberbucks/pay/250/")

    assert response.status_code == 200
    member.refresh_from_db()
    assert member.memberbucks_balance == 7.5
    debit = MemberBucks.objects.latest("id")
    assert debit.amount == -2.5
    assert debit.description == "No description. Manual payment via portal."


def test_a_payment_can_carry_the_members_own_description(make_member):
    from memberbucks.models import MemberBucks

    member = make_member(state="active", rfid="WALLET-PAY-2")
    credit(member, 10)

    fresh_client(member).post(
        "/api/memberbucks/pay/100/", {"description": "Laser time"}, format="json"
    )

    assert MemberBucks.objects.latest("id").description == "Laser time"


@pytest.mark.parametrize("cents", [0, 5001])
def test_a_payment_outside_the_topup_limit_is_rejected(make_member, cents):
    """The ceiling is ``MEMBERBUCKS_MAX_TOPUP`` dollars, converted to cents.

    A *payment* bounded by the *top-up* limit is a borrowed setting rather
    than a deliberate one, but it is what caps a single portal debit at $50.
    """
    member = make_member(state="active", rfid=f"WALLET-PAY-LIMIT-{cents}")
    credit(member, 1000)

    response = fresh_client(member).post(f"/api/memberbucks/pay/{cents}/")

    assert response.status_code == 400
    member.refresh_from_db()
    assert member.memberbucks_balance == 1000


def test_a_payment_larger_than_the_balance_is_rejected(make_member):
    from memberbucks.models import MemberBucks

    member = make_member(state="active", rfid="WALLET-PAY-3")
    credit(member, 10)

    response = fresh_client(member).post("/api/memberbucks/pay/1001/")

    assert response.status_code == 400
    assert response.data == "Not enough funds"
    assert MemberBucks.objects.count() == 1


def test_a_portal_payment_is_recorded_under_an_undeclared_type(make_member):
    """DEFECT, pinned: ``"web"`` is not one of ``TRANSACTION_TYPES``.

    The model declares stripe, bank, cash, card, interlock and other. Django
    does not enforce ``choices`` at the database, so the row saves -- but
    ``get_transaction_type_display()`` has no label to return and falls back
    to the raw value. Every other entry in a member's history reads "Cash" or
    "Stripe Top-up"; a portal payment reads "web".
    """
    member = make_member(state="active", rfid="WALLET-PAY-4")
    credit(member, 10)

    fresh_client(member).post("/api/memberbucks/pay/100/")

    from memberbucks.models import MemberBucks

    assert "web" not in dict(MemberBucks.TRANSACTION_TYPES)
    newest = fresh_client(member).get("/api/memberbucks/transactions/").json()[0]
    assert newest["type"] == "web"


def test_two_simultaneous_payments_can_overdraw_a_wallet(make_member, monkeypatch):
    """DEFECT, pinned: the balance check and the debit are not atomic.

    ``MemberBucksDonateFunds`` reads the balance, compares, and then writes.
    Nothing holds a lock between those steps, so two requests that arrive
    together can both read the old balance, both pass, and both debit.

    The interleaving is forced rather than left to chance: the second request
    is fired from inside the first one's write, after the first has passed its
    check but before its debit lands. Each request loads its own user, as real
    ones do. $10.00 and two $8.00 payments ends at -$6.00, with both answered
    200.

    The ledger itself stays consistent -- the balance is re-derived from it --
    so this is an overdraw, not a lost transaction. A member can only race
    their own wallet, but a double-tapped "pay" button is enough to do it.
    """
    from memberbucks.models import MemberBucks

    member = make_member(state="active", rfid="WALLET-RACE")
    credit(member, 10)

    original_save = MemberBucks.save
    second = {}

    def save_racing_a_second_request(self, *args, **kwargs):
        # Claimed before the request is sent, or the second request's own
        # debit would fire a third.
        if self.amount < 0 and not second:
            second["status"] = None
            second["status"] = (
                fresh_client(member).post("/api/memberbucks/pay/800/").status_code
            )
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(MemberBucks, "save", save_racing_a_second_request)

    first_status = fresh_client(member).post("/api/memberbucks/pay/800/").status_code

    member.refresh_from_db()
    assert (first_status, second["status"]) == (200, 200)
    assert member.memberbucks_balance == -6.0


# --------------------------------------------------------------------------
# /api/memberbucks/add/<dollars>/
# --------------------------------------------------------------------------


def test_a_top_up_charges_the_saved_card_in_cents_and_credits_dollars(
    member_with_card, stripe_stub, set_config
):
    from memberbucks.models import MemberBucks

    set_config(MEMBERBUCKS_CURRENCY="aud")
    stripe_stub.set("PaymentIntent.create", FakeStripeObject(status="succeeded"))

    response = fresh_client(member_with_card).post("/api/memberbucks/add/20/")

    assert response.status_code == 200
    ((_, _, charge),) = stripe_stub.calls_to("PaymentIntent.create")
    assert charge == {
        "amount": 2000,
        "currency": "aud",
        "customer": "cus_test",
        "payment_method": "pm_test",
        "off_session": True,
        "confirm": True,
    }
    member_with_card.refresh_from_db()
    assert member_with_card.memberbucks_balance == 20.0
    assert MemberBucks.objects.get().transaction_type == "stripe"


@pytest.mark.parametrize("dollars", [0, 51])
def test_a_top_up_outside_the_limit_never_reaches_stripe(
    member_with_card, stripe_stub, dollars
):
    response = fresh_client(member_with_card).post(f"/api/memberbucks/add/{dollars}/")

    assert response.status_code == 400
    assert not stripe_stub.called("PaymentIntent.create")


def test_a_card_needing_3d_secure_is_refused_without_crediting(
    member_with_card, stripe_stub
):
    from memberbucks.models import MemberBucks

    stripe_stub.set(
        "PaymentIntent.create", card_declined(code="authentication_required")
    )

    response = fresh_client(member_with_card).post("/api/memberbucks/add/10/")

    assert response.status_code == 400
    assert "3D Secure" in response.data
    assert not MemberBucks.objects.exists()


def test_a_declined_card_is_refused_without_crediting(member_with_card, stripe_stub):
    """The failed intent is re-fetched, but only so it can be logged."""
    from memberbucks.models import MemberBucks

    stripe_stub.set("PaymentIntent.create", card_declined())

    response = fresh_client(member_with_card).post("/api/memberbucks/add/10/")

    assert response.status_code == 400
    assert response.data == "Error charging card"
    ((_, args, _),) = stripe_stub.calls_to("PaymentIntent.retrieve")
    assert args == ("pi_declined",)
    assert not MemberBucks.objects.exists()


def test_an_unsettled_payment_is_refused_without_crediting(
    member_with_card, stripe_stub
):
    from memberbucks.models import MemberBucks

    stripe_stub.set("PaymentIntent.create", FakeStripeObject(status="requires_action"))

    response = fresh_client(member_with_card).post("/api/memberbucks/add/10/")

    assert response.status_code == 400
    assert not MemberBucks.objects.exists()


def test_a_member_with_no_saved_card_is_charged_anyway(make_member, stripe_stub):
    """DEFECT, pinned: nothing checks for a card before calling Stripe.

    A member who never saved one has empty strings for both ids, and those go
    to ``PaymentIntent.create`` as they are. Stripe answers with an
    ``InvalidRequestError``, which is not the ``CardError`` the view catches,
    so the member gets a 500 instead of being told to add a card.
    """
    member = make_member(state="active", rfid="WALLET-NOCARD")
    stripe_stub.set(
        "PaymentIntent.create",
        stripe.error.InvalidRequestError("No such customer: ''", "customer"),
    )

    with pytest.raises(stripe.error.InvalidRequestError):
        fresh_client(member).post("/api/memberbucks/add/10/")

    ((_, _, charge),) = stripe_stub.calls_to("PaymentIntent.create")
    assert (charge["customer"], charge["payment_method"]) == ("", "")


def test_a_top_up_is_attempted_even_with_stripe_disabled(
    member_with_card, stripe_stub, set_config
):
    """DEFECT, pinned: ``ENABLE_STRIPE`` only decides whether a key is set.

    ``StripeAPIView.__init__`` returns early when Stripe is disabled, skipping
    ``stripe.api_key = ...`` -- and that is all the flag does. The view goes on
    to create a payment intent regardless. Against the real SDK that fails
    for want of a key, as an ``AuthenticationError`` nothing catches; the stub
    here stands in for it so what is pinned is the attempt itself.
    """
    set_config(ENABLE_STRIPE=False)
    stripe_stub.set(
        "PaymentIntent.create", FakeStripeObject(status="requires_payment_method")
    )

    fresh_client(member_with_card).post("/api/memberbucks/add/10/")

    assert stripe_stub.called("PaymentIntent.create")


# --------------------------------------------------------------------------
# /api/memberbucks/balance-list/
# --------------------------------------------------------------------------


def test_the_balance_list_includes_every_profile_richest_first(as_admin, make_member):
    """Every profile, whatever its state -- including the admin reading it."""
    active = make_member(state="active", rfid="WALLET-LIST-2")
    inactive = make_member(state="inactive", rfid="WALLET-LIST-3")
    credit(active, 5)
    credit(inactive, 30)

    body = as_admin().get("/api/memberbucks/balance-list/").data
    rows = list(body["member_balances"])

    assert set(body) == {"total_memberbucks", "member_balances"}
    assert set(rows[0]) == BALANCE_LIST_ROW_FIELDS
    assert [row["memberbucks_balance"] for row in rows] == [30.0, 5.0, 0.0]


def test_the_circulation_total_silently_drops_large_balances(as_admin, make_member):
    """DEFECT, pinned: the total and the list disagree above 1,000.

    ``total_memberbucks`` is aggregated over ``memberbucks_balance__lt=1000``;
    the per-member list beneath it is not filtered at all. So a member holding
    1,000 or more appears in the list but is left out of the figure that
    claims to be the total in circulation. The endpoint's docstring promises
    "the total memberbucks in circulation", with no mention of a cut-off.

    The threshold may exist to exclude test or treasurer accounts, but it is
    undocumented, not configurable, and invisible in the response.
    """
    whale = make_member(state="active", rfid="WALLET-LIST-4")
    ordinary = make_member(state="active", rfid="WALLET-LIST-5")
    credit(whale, 1500)
    credit(ordinary, 20)

    body = as_admin().get("/api/memberbucks/balance-list/").data

    listed = sum(row["memberbucks_balance"] for row in body["member_balances"])
    assert listed == 1520.0
    assert body["total_memberbucks"] == 20.0
