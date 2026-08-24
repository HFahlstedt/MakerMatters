"""Characterisation: the memberbucks (vending) protocol.

Unlike doors and interlocks, memberbucks devices hold no cached tag list — they
resolve every card live over the socket and the server authorises each
transaction. Every active member with a tag is implicitly authorised.

These tests document a genuine units inconsistency in the debit path, so read
the assertions as "this is what it does today", not "this is correct".
"""

import pytest

from tests import ws

pytestmark = [pytest.mark.protocol, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def vending_with_member(make_memberbucks_device, make_member):
    def _build(serial="vend-01", balance_dollars=0.0, rfid="TAG-VEND", **device_kwargs):
        device = make_memberbucks_device(
            serial=serial, authorised=True, **device_kwargs
        )
        profile = make_member(state="active", rfid=rfid)
        if balance_dollars:
            from memberbucks.models import MemberBucks

            MemberBucks.objects.create(
                user=profile.user,
                amount=balance_dollars,
                transaction_type="cash",
                description="Test float",
            )
            profile.refresh_from_db()
        return device, profile

    return _build


def _clear_rate_limit(profile_pk, seconds=60):
    """Push ``last_memberbucks_purchase`` into the past.

    Necessary because the field defaults to ``timezone.now`` at profile
    creation, so a freshly created member is rate-limited out of their first
    purchase for three seconds.
    """

    def _do():
        from datetime import timedelta

        from django.utils import timezone

        from profile.models import Profile

        Profile.objects.filter(pk=profile_pk).update(
            last_memberbucks_purchase=timezone.now() - timedelta(seconds=seconds)
        )

    return _do


# --------------------------------------------------------------------------
# Balance
# --------------------------------------------------------------------------


async def test_balance_is_reported_in_cents(vending_with_member, device_api_key):
    await ws.aget(vending_with_member)(serial="vend-bal-01", balance_dollars=12.50)

    comm, _ = await ws.open_authenticated("memberbucks", "vend-bal-01", device_api_key)
    await comm.send_json_to({"command": "balance", "card_id": "TAG-VEND"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "balance",
        "balance": 1250,
        "success": True,
    }
    await comm.disconnect()


async def test_balance_for_unknown_card_is_refused(vending_with_member, device_api_key):
    await ws.aget(vending_with_member)(serial="vend-bal-02")

    comm, _ = await ws.open_authenticated("memberbucks", "vend-bal-02", device_api_key)
    await comm.send_json_to({"command": "balance", "card_id": "NOT-A-TAG"})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "balance",
        "reason": "invalid_card_id",
        "success": False,
    }
    await comm.disconnect()


async def test_balance_without_a_card_id_is_refused(
    vending_with_member, device_api_key
):
    await ws.aget(vending_with_member)(serial="vend-bal-03")

    comm, _ = await ws.open_authenticated("memberbucks", "vend-bal-03", device_api_key)
    await comm.send_json_to({"command": "balance"})

    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))[
        "reason"
    ] == "invalid_card_id"
    await comm.disconnect()


async def test_memberbucks_devices_are_never_sent_a_tag_list(
    vending_with_member, device_api_key
):
    """``sync_users`` short-circuits for anything that is not a door."""
    await ws.aget(vending_with_member)(serial="vend-nosync")

    comm, handshake = await ws.open_authenticated(
        "memberbucks", "vend-nosync", device_api_key
    )

    assert "sync" not in handshake["by_command"]
    await comm.disconnect()


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["debit", "credit"])
async def test_transaction_without_a_card_id_is_refused(
    vending_with_member, device_api_key, command
):
    await ws.aget(vending_with_member)(serial=f"vend-nocard-{command}")

    comm, _ = await ws.open_authenticated(
        "memberbucks", f"vend-nocard-{command}", device_api_key
    )
    await comm.send_json_to({"command": command, "amount": 100})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": command,
        "reason": "invalid_card_id",
        "success": False,
    }
    await comm.disconnect()


@pytest.mark.parametrize("command", ["debit", "credit"])
@pytest.mark.parametrize("amount", [0, -100])
async def test_non_positive_amounts_are_refused(
    vending_with_member, device_api_key, command, amount
):
    """Guards against a negative 'debit' silently crediting the member."""
    serial = f"vend-amt-{command}-{amount}"
    await ws.aget(vending_with_member)(serial=serial)

    comm, _ = await ws.open_authenticated("memberbucks", serial, device_api_key)
    await comm.send_json_to(
        {"command": command, "card_id": "TAG-VEND", "amount": amount}
    )

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": command,
        "reason": "invalid_amount",
        "success": False,
    }
    await comm.disconnect()


@pytest.mark.parametrize("command", ["debit", "credit"])
async def test_unknown_card_is_refused(vending_with_member, device_api_key, command):
    serial = f"vend-unknown-{command}"
    await ws.aget(vending_with_member)(serial=serial)

    comm, _ = await ws.open_authenticated("memberbucks", serial, device_api_key)
    await comm.send_json_to({"command": command, "card_id": "NOPE", "amount": 100})

    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))[
        "reason"
    ] == "invalid_card_id"
    await comm.disconnect()


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


async def test_purchases_are_rate_limited_to_one_per_three_seconds(
    vending_with_member, device_api_key, sent_emails
):
    _device, profile = await ws.aget(vending_with_member)(
        serial="vend-rate", balance_dollars=500
    )
    await ws.aget(_clear_rate_limit(profile.pk))()

    comm, _ = await ws.open_authenticated("memberbucks", "vend-rate", device_api_key)

    await comm.send_json_to({"command": "debit", "card_id": "TAG-VEND", "amount": 100})
    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["success"] is True

    # Second attempt immediately afterwards falls inside the 3s window.
    await comm.send_json_to({"command": "debit", "card_id": "TAG-VEND", "amount": 100})
    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "rate_limited"
    }
    await comm.disconnect()


async def test_a_brand_new_member_is_rate_limited_out_of_their_first_purchase(
    vending_with_member, device_api_key
):
    """DEFECT, pinned: ``last_memberbucks_purchase`` defaults to *now* at signup.

    The rate-limit check compares against it unconditionally, so a member who
    has never spent anything is blocked for the first three seconds of their
    existence. Harmless in practice, but it means "never purchased" and
    "purchased just now" are indistinguishable.
    """
    _device, _profile = await ws.aget(vending_with_member)(
        serial="vend-fresh", balance_dollars=500
    )

    comm, _ = await ws.open_authenticated("memberbucks", "vend-fresh", device_api_key)
    await comm.send_json_to({"command": "debit", "card_id": "TAG-VEND", "amount": 100})

    assert await comm.receive_json_from(timeout=ws.TIMEOUT) == {
        "command": "rate_limited"
    }
    await comm.disconnect()


# --------------------------------------------------------------------------
# Debit / credit
# --------------------------------------------------------------------------


async def test_debit_writes_a_card_transaction_and_returns_the_new_balance(
    vending_with_member, device_api_key, sent_emails
):
    """A 100 cent debit removes one dollar, and every amount on the wire is cents."""
    from memberbucks.models import MemberBucks

    _device, profile = await ws.aget(vending_with_member)(
        serial="vend-debit", balance_dollars=500
    )
    await ws.aget(_clear_rate_limit(profile.pk))()

    comm, _ = await ws.open_authenticated("memberbucks", "vend-debit", device_api_key)
    await comm.send_json_to({"command": "debit", "card_id": "TAG-VEND", "amount": 100})

    reply = await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    assert reply["command"] == "debit"
    assert reply["success"] is True
    assert reply["amount"] == -100  # cents
    assert reply["balance"] == 49900  # $499 remaining, in cents

    txn = await ws.aget(MemberBucks.objects.get)(
        user_id=profile.user_id, transaction_type="card"
    )
    assert txn.amount == pytest.approx(-1.0)


async def test_credit_increases_the_balance(
    vending_with_member, device_api_key, sent_emails
):
    from profile.models import Profile

    _device, profile = await ws.aget(vending_with_member)(
        serial="vend-credit", balance_dollars=10
    )
    await ws.aget(_clear_rate_limit(profile.pk))()

    comm, _ = await ws.open_authenticated("memberbucks", "vend-credit", device_api_key)
    await comm.send_json_to({"command": "credit", "card_id": "TAG-VEND", "amount": 500})
    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["success"] is True
    await comm.disconnect()

    refreshed = await ws.aget(Profile.objects.get)(pk=profile.pk)
    assert refreshed.memberbucks_balance == pytest.approx(15.0)


async def test_debit_notifies_the_member(
    vending_with_member, device_api_key, sent_emails
):
    _device, profile = await ws.aget(vending_with_member)(
        serial="vend-email", balance_dollars=500
    )
    # Resolve the FK in sync context; touching profile.user later would issue a
    # lazy query from the async test body.
    member_email = await ws.aget(lambda: profile.user.email)()
    await ws.aget(_clear_rate_limit(profile.pk))()

    comm, _ = await ws.open_authenticated("memberbucks", "vend-email", device_api_key)
    await comm.send_json_to({"command": "debit", "card_id": "TAG-VEND", "amount": 1})
    await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    assert len(sent_emails) == 1
    assert sent_emails[0]["To"] == member_email


async def test_insufficient_funds_are_refused(
    vending_with_member, device_api_key, sent_emails
):
    """A member holding $10 cannot spend $15."""
    from memberbucks.models import MemberBucks

    _device, profile = await ws.aget(vending_with_member)(
        serial="vend-insufficient", balance_dollars=10
    )
    await ws.aget(_clear_rate_limit(profile.pk))()

    comm, _ = await ws.open_authenticated(
        "memberbucks", "vend-insufficient", device_api_key
    )
    await comm.send_json_to({"command": "debit", "card_id": "TAG-VEND", "amount": 1500})

    reply = await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    assert reply["command"] == "debit"
    assert reply["success"] is False
    assert reply["reason"] == "insufficient_funds"
    assert reply["balance"] == 1000

    assert (
        await ws.aget(MemberBucks.objects.filter(transaction_type="card").count)() == 0
    )


async def test_a_member_with_ten_dollars_can_make_a_five_dollar_purchase(
    vending_with_member, device_api_key, sent_emails
):
    """The case the old cents/dollars mismatch got wrong in both directions.

    Before the fix a $10 balance was compared against 500 (cents) and the
    purchase was refused; had it gone through, it would have removed $500.
    """
    from profile.models import Profile

    _device, profile = await ws.aget(vending_with_member)(
        serial="vend-units", balance_dollars=10
    )
    await ws.aget(_clear_rate_limit(profile.pk))()

    comm, _ = await ws.open_authenticated("memberbucks", "vend-units", device_api_key)
    await comm.send_json_to({"command": "debit", "card_id": "TAG-VEND", "amount": 500})

    reply = await comm.receive_json_from(timeout=ws.TIMEOUT)
    await comm.disconnect()

    assert reply["success"] is True

    refreshed = await ws.aget(Profile.objects.get)(pk=profile.pk)
    assert refreshed.memberbucks_balance == pytest.approx(5.0)


async def test_a_product_purchase_logs_the_price_in_positive_cents(
    vending_with_member, device_api_key, sent_emails
):
    """``MemberbucksProductPurchaseLog.price`` is documented as cents.

    It previously received the signed dollar amount, which made it negative for
    every debit and inverted the admin screen's total-volume figure.
    """
    from memberbucks.models import MemberbucksProduct, MemberbucksProductPurchaseLog

    _device, profile = await ws.aget(vending_with_member)(
        serial="vend-product", balance_dollars=50
    )
    await ws.aget(_clear_rate_limit(profile.pk))()
    await ws.aget(MemberbucksProduct.objects.create)(
        name="Cola",
        external_id="A1",
        external_id_name="A1",
        price=250,
        cost_price=100,
        stock_level=10,
    )

    comm, _ = await ws.open_authenticated("memberbucks", "vend-product", device_api_key)
    await comm.send_json_to(
        {
            "command": "debit",
            "card_id": "TAG-VEND",
            "amount": 250,
            "product_external_id": "A1",
        }
    )
    assert (await comm.receive_json_from(timeout=ws.TIMEOUT))["success"] is True
    await comm.disconnect()

    log = await ws.aget(MemberbucksProductPurchaseLog.objects.get)(
        user_id=profile.user_id
    )
    assert log.price == 250
    assert log.cost_price == 100
