"""Characterisation tests for ``membermatters/oidc_provider_settings.py``.

This is what other systems (Vikunja, Moodle, ...) learn about a member when
they sign in through the portal's OpenID Connect provider: a standard
``userinfo`` claim set and two custom scopes, ``membershipinfo`` and
``vikunja_teams``. The unit tests call the hooks directly; the HTTP tests at
the end go through ``/api/openid/`` with a real token, to prove the settings
are wired in.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def claims_for(user, scopes):
    """The response ``CustomScopeClaims`` builds for a token with ``scopes``."""
    from membermatters.oidc_provider_settings import CustomScopeClaims

    token = SimpleNamespace(user=user, scope=scopes, client=None)
    return CustomScopeClaims(token).create_response_dic()


# --------------------------------------------------------------------------
# userinfo
# --------------------------------------------------------------------------


def test_userinfo_fills_the_standard_claims_from_the_profile(make_member):
    from membermatters.oidc_provider_settings import userinfo

    profile = make_member(state="active", phone="0400111222")
    claims = userinfo({}, profile.user)

    assert claims == {
        "name": profile.get_full_name(),
        "given_name": "Test",
        "family_name": profile.last_name,
        "nickname": profile.screen_name,
        "preferred_username": profile.screen_name,
        "email": profile.user.email,
        "email_verified": True,
        "phone_number": "0400111222",
        "phone_number_verified": False,
        "updated_at": profile.modified.isoformat(),
    }


def test_userinfo_leaves_out_blank_fields(make_member):
    """Used to send ``NO_FIRSTNAME``, ``NO_PHONENUMBER`` and so on, which a
    relying party creating an account from the claims stored as real values.
    Used also to send ``" "`` as the name, as two blank names join to a space."""
    from membermatters.oidc_provider_settings import userinfo

    profile = make_member(state="active")
    profile.first_name = ""
    profile.last_name = ""
    profile.screen_name = ""
    profile.phone = ""
    profile.save()

    claims = userinfo({}, profile.user)

    for claim in (
        "name",
        "given_name",
        "family_name",
        "nickname",
        "preferred_username",
        "phone_number",
    ):
        assert claim not in claims
    assert claims["email"] == profile.user.email


def test_userinfo_leaves_out_only_the_blank_fields(make_member):
    from membermatters.oidc_provider_settings import userinfo

    profile = make_member(state="active")
    profile.last_name = ""
    profile.phone = ""
    profile.save()

    claims = userinfo({}, profile.user)

    assert claims["given_name"] == "Test"
    assert claims["name"] == "Test"
    assert "family_name" not in claims


def test_phone_numbers_are_never_reported_as_verified(make_member):
    from membermatters.oidc_provider_settings import userinfo

    profile = make_member(state="active", phone="0400111222")

    assert userinfo({}, profile.user)["phone_number_verified"] is False


def test_an_unverified_email_is_reported_as_unverified(make_member):
    from membermatters.oidc_provider_settings import userinfo

    profile = make_member(state="active")
    profile.user.email_verified = False
    profile.user.save()

    assert userinfo({}, profile.user)["email_verified"] is False


# --------------------------------------------------------------------------
# membershipinfo
# --------------------------------------------------------------------------


def test_membershipinfo_for_an_active_subscribed_member(make_member):
    profile = make_member(state="active")
    first = timezone.now() - timedelta(days=90)
    profile.subscription_status = "active"
    profile.subscription_first_created = first
    profile.save()

    assert claims_for(profile.user, ["membershipinfo"]) == {
        "state": "active",
        "active": True,
        "subscriptionState": "active",
        "subscriptionActive": True,
        "firstSubscribedDate": first.isoformat(),
        "groups": ["active"],
    }


def test_membershipinfo_for_a_member_who_never_subscribed(make_member):
    profile = make_member(state="noob")

    claims = claims_for(profile.user, ["membershipinfo"])

    assert claims["active"] is False
    assert claims["subscriptionState"] == "inactive"
    assert claims["subscriptionActive"] is False
    # ``None`` values are stripped from a scope's response, so the key is
    # absent rather than null.
    assert "firstSubscribedDate" not in claims
    assert claims["groups"] == []


@pytest.mark.parametrize(
    "status, active",
    [
        ("active", True),
        ("cancelling", True),
        ("inactive", False),
        ("cancelled", False),
        ("past_due", False),
    ],
)
def test_only_active_and_cancelling_subscriptions_count_as_active(
    make_member, status, active
):
    profile = make_member(state="active")
    profile.subscription_status = status
    profile.save()

    claims = claims_for(profile.user, ["membershipinfo"])

    assert claims["subscriptionState"] == status
    assert claims["subscriptionActive"] is active


def test_a_subscription_does_not_make_an_inactive_member_active(make_member):
    """``active`` follows the member's state, not their subscription."""
    profile = make_member(state="inactive")
    profile.subscription_status = "active"
    profile.save()

    claims = claims_for(profile.user, ["membershipinfo"])

    assert claims["active"] is False
    assert claims["subscriptionActive"] is True
    assert claims["groups"] == []


def test_groups_list_staff_admin_superuser_and_active(make_member):
    profile = make_member(state="active")
    user = profile.user
    user.staff = True
    user.admin = True
    user.is_superuser = True
    user.save()

    claims = claims_for(user, ["membershipinfo"])

    assert claims["groups"] == ["staff", "admin", "superuser", "active"]


@pytest.mark.parametrize("flag", ["staff", "admin", "is_superuser"])
def test_each_privilege_flag_adds_only_its_own_group(make_member, flag):
    profile = make_member(state="inactive")
    setattr(profile.user, flag, True)
    profile.user.save()

    groups = claims_for(profile.user, ["membershipinfo"])["groups"]

    assert groups == [{"staff": "staff", "admin": "admin", "is_superuser": "superuser"}[flag]]


# --------------------------------------------------------------------------
# vikunja_teams
# --------------------------------------------------------------------------

TEAMS = [{"name": "Members", "oidcID": "members", "description": "x", "isPublic": False}]


def test_an_active_member_gets_the_configured_vikunja_teams(make_member, set_config):
    set_config(VIKUNJA_TEAMS=json.dumps(TEAMS))
    profile = make_member(state="active")

    assert claims_for(profile.user, ["vikunja_teams"]) == {"vikunja_groups": TEAMS}


@pytest.mark.parametrize("state", ["inactive", "noob", "accountonly"])
def test_a_member_who_is_not_active_gets_an_empty_team_list(
    make_member, set_config, state
):
    """An empty list is how a lapsed member is removed from the teams the next
    time they sign in."""
    set_config(VIKUNJA_TEAMS=json.dumps(TEAMS))
    profile = make_member(state=state)

    assert claims_for(profile.user, ["vikunja_teams"]) == {"vikunja_groups": []}


def test_no_vikunja_teams_configured_adds_no_claim(make_member, set_config):
    set_config(VIKUNJA_TEAMS="")
    profile = make_member(state="active")

    assert claims_for(profile.user, ["vikunja_teams"]) == {}


def test_invalid_vikunja_teams_json_adds_no_claim_and_logs(
    make_member, set_config, monkeypatch
):
    from membermatters import oidc_provider_settings

    errors = []
    monkeypatch.setattr(
        oidc_provider_settings.logger, "error", lambda msg, *a: errors.append(msg)
    )
    set_config(VIKUNJA_TEAMS="{not json")
    profile = make_member(state="active")

    assert claims_for(profile.user, ["vikunja_teams"]) == {}
    assert len(errors) == 1


@pytest.mark.parametrize("value", ['{"a": 1}', '"members"', "3", "null"])
def test_a_non_list_vikunja_teams_value_adds_no_claim(make_member, set_config, value):
    """Used to be passed to the relying party as ``vikunja_groups``; only the
    JSON syntax was checked."""
    set_config(VIKUNJA_TEAMS=value)
    profile = make_member(state="active")

    assert claims_for(profile.user, ["vikunja_teams"]) == {}


# --------------------------------------------------------------------------
# scopes
# --------------------------------------------------------------------------


def test_only_requested_scopes_contribute_claims(make_member, set_config):
    set_config(VIKUNJA_TEAMS=json.dumps(TEAMS))
    profile = make_member(state="active")

    assert "vikunja_groups" not in claims_for(profile.user, ["membershipinfo"])
    assert "state" not in claims_for(profile.user, ["vikunja_teams"])
    both = claims_for(profile.user, ["membershipinfo", "vikunja_teams"])
    assert "state" in both and "vikunja_groups" in both


def test_unknown_scopes_are_ignored(make_member):
    profile = make_member(state="active")

    assert claims_for(profile.user, ["nonsense"]) == {}


def test_the_custom_scopes_are_registered_with_descriptions():
    from membermatters.oidc_provider_settings import CustomScopeClaims

    assert CustomScopeClaims.info_membershipinfo[0] == "Membership Info"
    assert CustomScopeClaims.info_vikunja_teams[0] == "Vikunja Teams"


# --------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------


@pytest.fixture
def oidc_token(db):
    """A bearer token for ``profile.user`` with the given scopes."""
    from oidc_provider.models import Client, ResponseType, Token

    def _make(profile, scopes):
        client = Client.objects.create(
            name="Relying party",
            client_id="rp",
            client_secret="secret",
            _redirect_uris="https://rp.example/cb",
        )
        client.response_types.add(ResponseType.objects.get(value="code"))
        return Token.objects.create(
            user=profile.user,
            client=client,
            expires_at=timezone.now() + timedelta(hours=1),
            _scope=" ".join(scopes),
            access_token="tok-" + profile.user.email,
            _id_token=json.dumps({"sub": str(profile.user.pk)}),
        )

    return _make


def test_the_userinfo_endpoint_serves_the_configured_claims(
    api_client, make_member, set_config, oidc_token
):
    set_config(VIKUNJA_TEAMS=json.dumps(TEAMS))
    profile = make_member(state="active", phone="0400111222")
    token = oidc_token(profile, ["openid", "profile", "email", "phone", "membershipinfo", "vikunja_teams"])

    response = api_client.get(
        "/api/openid/userinfo", HTTP_AUTHORIZATION=f"Bearer {token.access_token}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == str(profile.user.pk)
    assert body["email"] == profile.user.email
    assert body["name"] == profile.get_full_name()
    assert body["phone_number"] == "0400111222"
    assert body["state"] == "active"
    assert body["groups"] == ["active"]
    assert body["vikunja_groups"] == TEAMS


def test_the_userinfo_endpoint_withholds_claims_that_were_not_granted(
    api_client, make_member, oidc_token
):
    profile = make_member(state="active")
    token = oidc_token(profile, ["openid", "email"])

    body = api_client.get(
        "/api/openid/userinfo", HTTP_AUTHORIZATION=f"Bearer {token.access_token}"
    ).json()

    assert body["email"] == profile.user.email
    assert "state" not in body
    assert "phone_number" not in body
    assert "name" not in body


def test_the_userinfo_endpoint_rejects_a_missing_or_unknown_token(api_client, db):
    assert api_client.get("/api/openid/userinfo").status_code == 401
    assert (
        api_client.get(
            "/api/openid/userinfo", HTTP_AUTHORIZATION="Bearer nope"
        ).status_code
        == 401
    )


def test_the_userinfo_endpoint_rejects_an_expired_token(
    api_client, make_member, oidc_token
):
    profile = make_member(state="active")
    token = oidc_token(profile, ["openid"])
    token.expires_at = timezone.now() - timedelta(minutes=1)
    token.save()

    response = api_client.get(
        "/api/openid/userinfo", HTTP_AUTHORIZATION=f"Bearer {token.access_token}"
    )

    assert response.status_code == 401


def test_the_discovery_document_points_at_the_portals_endpoints(api_client, db):
    """Pinned as behaviour: the document does not list ``scopes_supported``, so
    the custom scopes are not discoverable and a relying party has to be told
    to ask for ``membershipinfo`` / ``vikunja_teams`` out of band."""
    response = api_client.get("/api/openid/.well-known/openid-configuration")

    assert response.status_code == 200
    document = response.json()
    assert document["issuer"].endswith("/api/openid")
    assert document["userinfo_endpoint"].endswith("/api/openid/userinfo")
    assert document["token_endpoint"].endswith("/api/openid/token")
    assert "scopes_supported" not in document
