"""Characterisation: the parts of ``api_general`` the other suites do not reach.

``test_auth_and_session.py`` covers login, registration, verification, password
reset, the profile and kiosks, and the site-session endpoints. What was left
is the edges: the site config's fallbacks for hand-edited JSON, the Mailchimp
leg of registration, the sign-out chat notification, and the small supporting
modules (the model-permission map, the Celery failure hook, the kiosk model).
"""

import json
import os
import sys
import types

import pytest
import requests

pytestmark = pytest.mark.django_db


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


# --------------------------------------------------------------------------
# GET /api/config/
# --------------------------------------------------------------------------


def test_malformed_home_page_cards_fall_back_to_an_error_card(api_client, set_config):
    """The setting is free text in the admin; bad JSON must not break the page."""
    set_config(HOME_PAGE_CARDS="[{ this is not json")

    body = api_client.get("/api/config/").json()

    (card,) = body["homepageCards"]
    assert card["title"] == "Error loading configuration"
    assert card["url"] == "#"


def test_malformed_webcam_urls_fall_back_to_an_error_entry(api_client, set_config):
    set_config(WEBCAM_PAGE_URLS="not json either")

    body = api_client.get("/api/config/").json()

    assert body["webcamLinks"] == [["Error Loading Webcam Configuration", ""]]


def test_the_site_config_does_not_depend_on_the_working_directory(
    api_client, tmp_path, monkeypatch
):
    """Used to be a 500 unless the server was started from ``memberportal/``.

    The version was read from ``open("../package.json")``, relative to the
    process's working directory, so a service manager's default directory made
    the endpoint the frontend loads first fail and the whole portal unusable.
    The path is now built from ``BASE_DIR``.
    """
    monkeypatch.chdir(tmp_path)

    response = api_client.get("/api/config/")

    assert response.status_code == 200
    assert response.json()["version"]


# --------------------------------------------------------------------------
# POST /api/register/: the Mailchimp leg
# --------------------------------------------------------------------------


@pytest.fixture
def mailchimp(monkeypatch, set_config):
    """A stand-in ``mailchimp_marketing`` that records what it is asked."""
    calls = {"config": None, "members": []}

    class Lists:
        def __init__(self, fail):
            self.fail = fail

        def add_list_member(self, list_id, payload):
            if self.fail:
                raise RuntimeError("Member Exists")
            calls["members"].append((list_id, payload))

    class Client:
        fail = False

        def __init__(self):
            self.lists = Lists(Client.fail)

        def set_config(self, config):
            calls["config"] = config

    module = types.ModuleType("mailchimp_marketing")
    module.Client = Client
    api_client_module = types.ModuleType("mailchimp_marketing.api_client")
    api_client_module.ApiClientError = type("ApiClientError", (Exception,), {})
    module.api_client = api_client_module
    monkeypatch.setitem(sys.modules, "mailchimp_marketing", module)
    monkeypatch.setitem(sys.modules, "mailchimp_marketing.api_client", api_client_module)
    set_config(
        MAILCHIMP_API_KEY="key-us1",
        MAILCHIMP_SERVER="us1",
        MAILCHIMP_LIST_ID="list-9",
        MAILCHIMP_TAG="Member",
    )
    calls["Client"] = Client
    return calls


def registration():
    return {
        "email": "Chimp@Example.com",
        "password": "a-long-password-1",
        "firstName": "Chimp",
        "lastName": "Test",
        "screenName": "chimpy",
        "mobile": "0400000000",
        "vehicleRegistrationPlate": "ABC123",
    }


def test_registering_subscribes_the_new_member_to_mailchimp(
    api_client, sent_emails, mailchimp
):
    response = api_client.post("/api/register/", registration(), format="json")

    assert response.status_code == 200
    assert mailchimp["config"] == {"api_key": "key-us1", "server": "us1"}
    ((list_id, payload),) = mailchimp["members"]
    assert list_id == "list-9"
    assert payload == {
        "email_address": "chimp@example.com",
        "email_type": "html",
        "status": "subscribed",
        "merge_fields": {"FNAME": "Chimp", "LNAME": "Test", "PHONE": "0400000000"},
        "vip": False,
        "tags": ["Member"],
    }


def test_a_mailchimp_failure_does_not_fail_the_registration(
    api_client, sent_emails, mailchimp, monkeypatch
):
    """Reported to Sentry and logged; the account has been created and stays."""
    from profile.models import User

    mailchimp["Client"].fail = True
    seen = []
    monkeypatch.setattr("api_general.views.sentry_sdk.capture_exception", seen.append)

    response = api_client.post("/api/register/", registration(), format="json")

    assert response.status_code == 200
    assert User.objects.filter(email="chimp@example.com").exists()
    assert len(seen) == 1


def test_mailchimp_is_not_contacted_without_an_api_key(
    api_client, sent_emails, mailchimp, set_config
):
    set_config(MAILCHIMP_API_KEY="")

    api_client.post("/api/register/", registration(), format="json")

    assert mailchimp["members"] == []


# --------------------------------------------------------------------------
# Site sign-in / sign-out: the chat notifications
# --------------------------------------------------------------------------


@pytest.fixture
def chat_posts(monkeypatch):
    posts = []
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json=None, **kwargs: posts.append((url, json)),
    )
    return posts


def test_signing_in_posts_to_discord_when_it_is_configured(
    make_member, as_member, set_config, chat_posts
):
    set_config(
        ENABLE_DISCORD_INTEGRATION=True, DISCORD_DOOR_WEBHOOK="https://discord.example/door"
    )
    member = make_member(state="active", rfid="CHAT-1")

    as_member(member).post("/api/sitesessions/signin/", {"guests": "[]"}, format="json")

    ((url, body),) = chat_posts
    assert url == "https://discord.example/door"
    assert "Test Member1 just signed in at a kiosk" in body["embeds"][0]["description"]


def test_signing_out_posts_to_discord_without_needing_a_slack_webhook(
    make_member, as_member, set_config, chat_posts
):
    """Used to check the wrong integration's webhook.

    ``SiteSignOut`` guarded its Discord post with
    ``config.ENABLE_DISCORD_INTEGRATION and config.SLACK_DOOR_WEBHOOK``, so a
    space with Discord configured and no Slack saw every sign-in announced and
    no sign-out. Each notifier now checks its own settings.
    """
    set_config(
        ENABLE_DISCORD_INTEGRATION=True,
        DISCORD_DOOR_WEBHOOK="https://discord.example/door",
        SLACK_DOOR_WEBHOOK="",
    )
    member = make_member(state="active", rfid="CHAT-2")
    client = as_member(member)
    client.post("/api/sitesessions/signin/", {"guests": "[]"}, format="json")
    chat_posts.clear()

    client.put("/api/sitesessions/signout/")

    ((url, body),) = chat_posts
    assert url == "https://discord.example/door"
    assert "signed out" in body["embeds"][0]["description"]


def test_kiosk_sign_ins_and_outs_are_posted_to_slack(
    make_member, as_member, set_config, chat_posts
):
    """Used to be silent: ``post_kiosk_swipe_to_slack`` was imported and never
    called, so a Slack-only space got no announcements at all."""
    set_config(
        ENABLE_SLACK_INTEGRATION=True,
        SLACK_DOOR_WEBHOOK="https://slack.example/door",
        ENABLE_DISCORD_INTEGRATION=False,
    )
    member = make_member(state="active", rfid="CHAT-3")
    client = as_member(member)

    client.post("/api/sitesessions/signin/", {"guests": "[]"}, format="json")
    client.put("/api/sitesessions/signout/")

    assert [(url, body["text"]) for url, body in chat_posts] == [
        ("https://slack.example/door", ":book: Test Member1 just signed in at a kiosk."),
        ("https://slack.example/door", ":book: Test Member1 just signed out at a kiosk."),
    ]


def test_signing_out_resyncs_the_members_devices(
    make_member, as_member, make_door, make_interlock, capture_channel_sends
):
    member = make_member(state="active", rfid="CHAT-4")
    member.doors.add(make_door(serial="out-door"))
    member.interlocks.add(make_interlock(serial="out-lock"))

    as_member(member).put("/api/sitesessions/signout/")

    assert "sync_users" in capture_channel_sends


# --------------------------------------------------------------------------
# Small endpoints
# --------------------------------------------------------------------------


def test_loggedin_is_401_for_an_anonymous_caller_and_200_for_a_member(
    api_client, make_member, as_member
):
    assert api_client.get("/api/loggedin/").status_code == 401

    member = make_member(state="active", rfid="LOGGED-1")
    assert as_member(member).get("/api/loggedin/").status_code == 200


def test_validating_an_unknown_reset_token_says_not_valid(api_client):
    response = api_client.post(
        "/api/password/reset/",
        {"token": "5f1c3b9e-0000-4000-8000-000000000000"},
        format="json",
    )

    assert response.json() == {"success": False}


@pytest.mark.parametrize(
    "body", [{"token": "no-such-token"}, {"token": "no-such-token", "password": "x"}]
)
def test_a_reset_token_that_is_not_a_uuid_is_not_valid(api_client, body):
    """Used to raise ``ValidationError``: the token went straight into a
    UUID-field lookup and only ``DoesNotExist`` was caught, so a truncated link
    or a typo answered 500 instead of "not valid"."""
    response = api_client.post("/api/password/reset/", body, format="json")

    assert response.status_code == 200
    assert response.json() == {"success": False}


# --------------------------------------------------------------------------
# Models, permissions and the Celery hook
# --------------------------------------------------------------------------


def test_a_kiosk_is_unavailable_after_five_silent_minutes(make_kiosk):
    from datetime import timedelta

    from django.utils import timezone

    kiosk = make_kiosk(kiosk_id="avail-1")
    assert kiosk.get_unavailable() is False  # never seen: not "unavailable"

    kiosk.checkin()
    assert kiosk.get_unavailable() is False

    kiosk.last_seen = timezone.now() - timedelta(minutes=6)
    assert kiosk.get_unavailable() is True
    assert str(kiosk) == kiosk.name


def test_a_site_session_describes_itself_and_signs_out(make_member):
    from api_general.models import SiteSession

    member = make_member(state="active", rfid="SESS-1")
    session = SiteSession.objects.create(user=member.user)

    assert session.signout_date is None
    session.signout()
    session.refresh_from_db()
    assert session.signout_date is not None
    assert str(session).startswith("Test Member1 - in: ")


def test_the_model_permission_map_requires_view_for_get_and_nothing_for_options():
    """``DjangoModelPermissions`` lets any authenticated user GET; this one does not."""
    from api_general.permissions import DjangoModelPermissionsWithRead

    perms = DjangoModelPermissionsWithRead.perms_map
    assert perms["GET"] == ["%(app_label)s.view_%(model_name)s"]
    assert perms["OPTIONS"] == perms["HEAD"] == []
    assert perms["POST"] == ["%(app_label)s.add_%(model_name)s"]
    assert perms["PATCH"] == perms["PUT"] == ["%(app_label)s.change_%(model_name)s"]
    assert perms["DELETE"] == ["%(app_label)s.delete_%(model_name)s"]


def test_a_failed_celery_task_is_logged_with_its_traceback(monkeypatch):
    from api_general import tasks

    lines = []
    monkeypatch.setattr(tasks.logger, "error", lines.append)

    try:
        raise ValueError("boom")
    except ValueError as error:
        tb = error.__traceback__

    sender = types.SimpleNamespace(request=types.SimpleNamespace(correlation_id="corr-1"))
    tasks.on_task_failure(
        sender=sender, task_id="task-7", traceback=tb, exception=ValueError("boom")
    )

    (line,) = lines
    assert line.startswith("[task:task-7:corr-1]")
    assert "test_a_failed_celery_task_is_logged_with_its_traceback" in line
    assert line.endswith("boom")
