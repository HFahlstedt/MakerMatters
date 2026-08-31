"""Characterisation: admin management of membership tiers, plans and settings.

Tiers and plans are mirrored into Stripe as Products and Prices, created
synchronously on the request path with no reconciliation if the two drift.

The important thing recorded here is that **plan cost is denominated
differently on every side of the admin API**: the read path divides by 100, the
create path expects cents, and the update path stores whatever it is given.
That combination makes an edit through the admin UI destructive, which is
pinned below.
"""

import pytest

from tests.conftest import FakeStripeObject

pytestmark = pytest.mark.django_db

TIER_FIELDS = {"id", "name", "description", "visible", "featured", "stripeId"}
PLAN_FIELDS = {
    "id",
    "name",
    "stripeId",
    "memberTier",
    "visible",
    "currency",
    "cost",
    "intervalCount",
    "interval",
}


# --------------------------------------------------------------------------
# Tiers
# --------------------------------------------------------------------------


def test_tier_list_shape(as_admin, make_tier_and_plan, set_config):
    set_config(ENABLE_STRIPE=True)
    tier, _plan = make_tier_and_plan()

    body = as_admin().get("/api/admin/tiers/").json()

    assert len(body) == 1
    assert set(body[0]) == TIER_FIELDS
    assert body[0]["stripeId"] == tier.stripe_id


def test_hidden_tiers_are_visible_to_admins(as_admin, set_config):
    """Unlike the member-facing endpoint, the admin list is unfiltered."""
    from api_admin_tools.models import MemberTier

    set_config(ENABLE_STRIPE=True)
    MemberTier.objects.create(
        name="Hidden", description="Not for members", stripe_id="prod_h", visible=False
    )

    body = as_admin().get("/api/admin/tiers/").json()

    assert [t["name"] for t in body] == ["Hidden"]
    assert body[0]["visible"] is False


def test_fetching_a_single_tier(as_admin, make_tier_and_plan, set_config):
    set_config(ENABLE_STRIPE=True)
    tier, _plan = make_tier_and_plan()

    body = as_admin().get(f"/api/admin/tiers/{tier.id}/").json()

    assert set(body) == TIER_FIELDS
    assert body["id"] == tier.id


def test_fetching_an_unknown_tier_is_a_404(as_admin, set_config):
    set_config(ENABLE_STRIPE=True)

    assert as_admin().get("/api/admin/tiers/9999/").status_code == 404


def test_creating_a_tier_also_creates_a_stripe_product(
    as_admin, stripe_stub, set_config
):
    from api_admin_tools.models import MemberTier

    set_config(ENABLE_STRIPE=True)
    stripe_stub.set("Product.create", FakeStripeObject({"id": "prod_new"}))

    body = (
        as_admin()
        .post(
            "/api/admin/tiers/",
            {
                "name": "Concession",
                "description": "Reduced rate",
                "visible": True,
                "featured": False,
            },
            format="json",
        )
        .json()
    )

    assert body["stripeId"] == "prod_new"
    assert MemberTier.objects.get(pk=body["id"]).name == "Concession"

    _path, _args, kwargs = stripe_stub.calls_to("Product.create")[0]
    assert kwargs == {"name": "Concession", "description": "Reduced rate"}


def test_a_stripe_auth_failure_when_creating_a_tier_is_reported(
    as_admin, stripe_stub, set_config
):
    import stripe

    set_config(ENABLE_STRIPE=True)
    stripe_stub.set("Product.create", stripe.error.AuthenticationError("no key"))

    response = as_admin().post(
        "/api/admin/tiers/",
        {"name": "X", "description": "Y", "visible": True, "featured": False},
        format="json",
    )

    assert response.status_code == 500
    assert response.json() == {"success": False, "message": "error.stripeNotConfigured"}


def test_updating_a_tier_does_not_touch_stripe(
    as_admin, make_tier_and_plan, stripe_stub, set_config
):
    """Renaming a tier locally leaves the Stripe Product name stale."""
    from api_admin_tools.models import MemberTier

    set_config(ENABLE_STRIPE=True)
    tier, _plan = make_tier_and_plan()

    as_admin().put(
        f"/api/admin/tiers/{tier.id}/",
        {
            "name": "Renamed Tier",
            "description": "New description",
            "visible": False,
            "featured": True,
        },
        format="json",
    )

    refreshed = MemberTier.objects.get(pk=tier.id)
    assert refreshed.name == "Renamed Tier"
    assert refreshed.featured is True
    assert not stripe_stub.called("Product.create")


def test_deleting_a_tier_leaves_the_stripe_product_behind(
    as_admin, make_tier_and_plan, stripe_stub, set_config
):
    from api_admin_tools.models import MemberTier

    set_config(ENABLE_STRIPE=True)
    tier, _plan = make_tier_and_plan()

    assert as_admin().delete(f"/api/admin/tiers/{tier.id}/").status_code == 200
    assert MemberTier.objects.count() == 0


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


def test_plan_list_shape(as_admin, make_tier_and_plan, set_config):
    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan()

    body = as_admin().get("/api/admin/plans/").json()

    assert set(body[0]) == PLAN_FIELDS
    assert body[0]["memberTier"] == plan.member_tier_id


def test_plans_can_be_listed_for_one_tier(as_admin, make_tier_and_plan, set_config):
    set_config(ENABLE_STRIPE=True)
    tier, plan = make_tier_and_plan()

    body = as_admin().get(f"/api/admin/tiers/{tier.id}/plans/").json()

    assert [p["id"] for p in body] == [plan.id]


def test_both_read_paths_report_cost_in_the_same_unit(
    as_admin, make_tier_and_plan, set_config
):
    """``/api/admin/plans/`` and ``/api/billing/tiers/`` agree.

    ``PaymentPlan.cost`` is cents, and so are ``PaymentPlan.get_object()`` and
    the admin create path. ``ManageMembershipTierPlan.get_plan()`` used to be
    the sole exception, dividing by 100, so the same plan reported two
    different numbers depending on which endpoint you asked.
    """
    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan(cost=2500)

    admin_view = as_admin().get(f"/api/admin/plans/{plan.id}/").json()
    member_view = as_admin().get("/api/billing/tiers/").json()[0]["plans"][0]

    assert admin_view["cost"] == 2500  # cents
    assert member_view["cost"] == 2500  # cents


def test_creating_a_plan_also_creates_a_stripe_price(
    as_admin, make_tier_and_plan, stripe_stub, set_config
):
    from api_admin_tools.models import PaymentPlan

    set_config(ENABLE_STRIPE=True)
    tier, _plan = make_tier_and_plan()
    stripe_stub.set("Price.create", FakeStripeObject({"id": "price_new"}))

    body = (
        as_admin()
        .post(
            "/api/admin/plans/",
            {
                "name": "Quarterly",
                "memberTier": tier.id,
                "visible": True,
                "currency": "AUD",
                "cost": 7500,
                "intervalCount": 3,
                "interval": "month",
            },
            format="json",
        )
        .json()
    )

    created = PaymentPlan.objects.get(pk=body["id"])
    assert created.cost == 7500  # the create path takes cents
    assert created.currency == "aud"  # lowercased on the way in

    _path, _args, kwargs = stripe_stub.calls_to("Price.create")[0]
    assert kwargs["unit_amount"] == 7500
    assert kwargs["currency"] == "aud"
    assert kwargs["recurring"] == {"interval": "month", "interval_count": 3}
    assert kwargs["product"] == tier.stripe_id


def test_editing_a_plan_and_saving_it_back_leaves_the_price_alone(
    as_admin, make_tier_and_plan, set_config
):
    """The read and write paths agree about units.

    ``get_plan()`` used to return ``cost / 100`` while ``put()`` stored
    ``body["cost"]`` verbatim, so loading a plan and saving it back unchanged
    turned 2500 cents ($25.00) into 25 cents — and repeating the edit kept
    dividing.
    """
    from api_admin_tools.models import PaymentPlan

    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan(cost=2500)
    client = as_admin()

    loaded = client.get(f"/api/admin/plans/{plan.id}/").json()
    assert loaded["cost"] == 2500

    # Save it back untouched, exactly as an edit form would.
    client.put(
        f"/api/admin/plans/{plan.id}/",
        {"name": loaded["name"], "visible": loaded["visible"], "cost": loaded["cost"]},
        format="json",
    )

    assert PaymentPlan.objects.get(pk=plan.id).cost == 2500


def test_updating_a_plan_does_not_touch_stripe(
    as_admin, make_tier_and_plan, stripe_stub, set_config
):
    """The local price changes but the Stripe Price is immutable and untouched.

    Members keep being billed the original amount.
    """
    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan(cost=2500)

    as_admin().put(
        f"/api/admin/plans/{plan.id}/",
        {"name": "Monthly", "visible": True, "cost": 9900},
        format="json",
    )

    assert not stripe_stub.called("Price.create")


def test_posting_a_plan_to_the_tier_scoped_url_is_rejected(
    as_admin, make_tier_and_plan, set_config
):
    set_config(ENABLE_STRIPE=True)
    tier, _plan = make_tier_and_plan()

    response = as_admin().post(f"/api/admin/tiers/{tier.id}/plans/", {}, format="json")

    assert response.status_code == 400


def test_deleting_a_plan(as_admin, make_tier_and_plan, set_config):
    from api_admin_tools.models import PaymentPlan

    set_config(ENABLE_STRIPE=True)
    _tier, plan = make_tier_and_plan()

    assert as_admin().delete(f"/api/admin/plans/{plan.id}/").status_code == 200
    assert PaymentPlan.objects.count() == 0


# --------------------------------------------------------------------------
# Runtime settings
# --------------------------------------------------------------------------


def test_the_settings_list_only_contains_customised_values(as_admin, set_config):
    """Constance writes a row only when a value is changed from its default.

    So this endpoint exposes overrides, not the full 109-key configuration. A
    refactor that reads from ``CONSTANCE_CONFIG`` instead would change both the
    contents and the size of this response.
    """
    set_config(SITE_NAME="Overridden Space")

    body = as_admin().get("/api/admin/settings/").json()

    keys = {row["key"] for row in body}
    assert "SITE_NAME" in keys
    assert "SITE_OWNER" not in keys  # untouched, so no row exists
    assert set(body[0]) == {"key", "value"}


def test_reading_a_customised_setting(as_admin, set_config):
    set_config(SITE_NAME="Readable Space")

    body = as_admin().get("/api/admin/settings/SITE_NAME/").json()

    assert body == {"key": "SITE_NAME", "value": "Readable Space"}


def test_reading_a_default_setting_is_a_404(as_admin):
    """A key at its shipped default has no database row, so it cannot be read."""
    assert as_admin().get("/api/admin/settings/SITE_OWNER/").status_code == 404


def test_updating_a_setting_changes_the_live_config(as_admin, set_config):
    from constance import config

    set_config(SITE_NAME="Before")

    body = (
        as_admin()
        .put("/api/admin/settings/SITE_NAME/", {"value": "After"}, format="json")
        .json()
    )

    assert body == {"key": "SITE_NAME", "value": "After"}
    assert config.SITE_NAME == "After"


def test_updating_an_unknown_setting_is_a_404(as_admin):
    response = as_admin().put(
        "/api/admin/settings/NOT_A_REAL_SETTING/", {"value": "x"}, format="json"
    )

    assert response.status_code == 404


def test_updating_without_a_key_is_rejected(as_admin):
    assert (
        as_admin()
        .put("/api/admin/settings/", {"value": "x"}, format="json")
        .status_code
        == 400
    )


def test_a_setting_rejects_a_value_of_the_wrong_type(as_admin, set_config):
    """The endpoint used to write whatever type it was given.

    ``ENABLE_STRIPE`` is declared as a boolean, but a string round-tripped
    through the codec unchanged. Every ``config.ENABLE_STRIPE`` check in the
    codebase then read a non-empty string as truthy, so the string "false"
    silently *enabled* Stripe.
    """
    from constance import config

    set_config(ENABLE_STRIPE=True)

    response = as_admin().put(
        "/api/admin/settings/ENABLE_STRIPE/", {"value": "false"}, format="json"
    )

    assert response.status_code == 400
    assert config.ENABLE_STRIPE is True


def test_a_setting_accepts_a_value_of_the_declared_type(as_admin, set_config):
    from constance import config

    set_config(ENABLE_STRIPE=True)

    response = as_admin().put(
        "/api/admin/settings/ENABLE_STRIPE/", {"value": False}, format="json"
    )

    assert response.status_code == 200
    assert config.ENABLE_STRIPE is False


def test_an_integer_is_accepted_for_a_setting_declared_as_a_float(as_admin, set_config):
    """JSON does not distinguish 2 from 2.0, so the check must not either."""
    from django.conf import settings as django_settings

    float_keys = [
        key
        for key, declared in django_settings.CONSTANCE_CONFIG.items()
        if isinstance(declared[0], float)
    ]
    assert float_keys, "expected at least one float setting to exercise this"
    key = float_keys[0]

    set_config(**{key: 1.5})

    assert (
        as_admin()
        .put(f"/api/admin/settings/{key}/", {"value": 2}, format="json")
        .status_code
        == 200
    )


def test_settings_are_returned_decoded_not_as_a_storage_envelope(as_admin, set_config):
    """Regression guard for the django-constance 2 -> 4 upgrade.

    Constance 4 keeps values as a JSON envelope in a plain TextField, whereas
    2.x used a PickledObjectField that decoded on attribute access. Reading
    ``setting.value`` straight off the model therefore started returning
    '{"__type__": "default", "__value__": "..."}' to the admin UI. The view
    now decodes explicitly.
    """
    from constance.models import Constance

    set_config(SITE_NAME="Envelope Check")

    stored = Constance.objects.get(key="SITE_NAME").value
    assert "__value__" in stored  # this is what the column really holds

    body = as_admin().get("/api/admin/settings/SITE_NAME/").json()
    assert body["value"] == "Envelope Check"


@pytest.mark.parametrize(
    "key,value",
    [
        ("SITE_NAME", "A String"),
        ("ENABLE_WEBCAMS", True),
        ("MEMBERBUCKS_MAX_TOPUP", "75"),
        ("MIN_INDUCTION_SCORE", 80),
    ],
)
def test_settings_round_trip_through_the_api(as_admin, set_config, key, value):
    """What is written must read back identically, and reach ``config``."""
    from constance import config

    set_config(**{key: getattr(config, key)})  # ensure a row exists
    client = as_admin()

    client.put(f"/api/admin/settings/{key}/", {"value": value}, format="json")

    assert client.get(f"/api/admin/settings/{key}/").json()["value"] == value
    assert getattr(config, key) == value


def test_settings_endpoints_reject_non_staff(make_member, as_member):
    member = make_member(state="active", rfid="SETTINGS-1")

    assert as_member(member).get("/api/admin/settings/").status_code == 403
