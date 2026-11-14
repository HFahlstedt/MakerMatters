"""Characterisation: where a reported issue goes.

``POST /api/tools/issue/`` fans one member's report out to up to four places --
a Vikunja task, a Trello card, an email to the committee and a Discord post --
and ``test_member_tools.py`` covers the validation in front of it and the email
leg. This file is the other three legs and the way they combine.

The shape worth knowing before reading: each integration runs in its own
``try`` so that one failing does not stop the next, a failure of any of them
makes the whole request a 500, and the Discord leg is the one that is not
wrapped. A 500 therefore does not mean nothing was delivered. A member who sees
it and presses submit again sends the report a second time to every place that
had already worked.

Nothing here talks to a network: ``requests.request`` is replaced by
``vikunja_and_trello``, which records each call.
"""

import pytest
import requests

pytestmark = pytest.mark.django_db

URL = "/api/tools/issue/"
REPORT = {"title": "Lathe is broken", "description": "Grinding noise"}


class Reply:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = {} if body is None else body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Network:
    """Records ``requests.request`` calls and answers them from a script.

    ``answers`` maps ``(method, url-suffix)`` to a ``Reply`` or an exception to
    raise; an unscripted call is a test failure rather than a silent success.
    """

    def __init__(self):
        self.calls = []
        self.answers = {}

    def answer(self, method, suffix, reply):
        self.answers[(method, suffix)] = reply

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), reply in self.answers.items():
            if m == method and url.endswith(suffix):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise AssertionError(f"unscripted {method} {url}")

    def to(self, suffix):
        return [c for c in self.calls if c[1].endswith(suffix)]


@pytest.fixture
def network(monkeypatch):
    net = Network()
    monkeypatch.setattr(requests, "request", net)
    return net


@pytest.fixture
def reporter(make_member, as_member):
    return as_member(make_member(state="active", rfid="ISSUE-INT"))


@pytest.fixture
def vikunja(set_config):
    """Vikunja switched on and configured; the other integrations off."""
    set_config(
        REPORT_ISSUE_ENABLE_EMAIL=False,
        REPORT_ISSUE_ENABLE_DISCORD=False,
        REPORT_ISSUE_ENABLE_TRELLO=False,
        REPORT_ISSUE_ENABLE_VIKUNJA=True,
        VIKUNJA_API_URL="https://tasks.example.org",
        VIKUNJA_API_TOKEN="vik-token",
        VIKUNJA_DEFAULT_PROJECT_ID="7",
        VIKUNJA_DEFAULT_LABEL_ID="3",
    )


@pytest.fixture
def trello(set_config):
    set_config(
        REPORT_ISSUE_ENABLE_EMAIL=False,
        REPORT_ISSUE_ENABLE_DISCORD=False,
        REPORT_ISSUE_ENABLE_VIKUNJA=False,
        REPORT_ISSUE_ENABLE_TRELLO=True,
        TRELLO_API_KEY="trello-key",
        TRELLO_API_TOKEN="trello-token",
        TRELLO_ID_LIST="list-1",
    )


def vikunja_ok(network, task_id=55):
    network.answer("PUT", "/projects/7/tasks", Reply(201, {"id": task_id}))
    network.answer("PUT", f"/tasks/{task_id}/labels", Reply(201, {}))


# --------------------------------------------------------------------------
# Vikunja
# --------------------------------------------------------------------------


def test_a_report_becomes_a_vikunja_task_with_the_configured_label(
    reporter, network, vikunja
):
    vikunja_ok(network)

    response = reporter.post(URL, REPORT, format="json")

    assert response.status_code == 201
    task, label = network.calls
    assert task[0:2] == ("PUT", "https://tasks.example.org/api/v1/projects/7/tasks")
    assert task[2]["headers"] == {"Authorization": "Bearer vik-token"}
    assert task[2]["json"]["title"] == "Lathe is broken"
    # The reporter's name is prepended to the description they typed.
    assert task[2]["json"]["description"] == "Test Member1: Grinding noise"
    assert task[2]["json"]["project_id"] == 7
    assert label[0:2] == ("PUT", "https://tasks.example.org/api/v1/tasks/55/labels")
    assert label[2]["json"]["label_id"] == 3


def test_vikunja_with_no_default_project_is_skipped_silently(
    reporter, network, set_config, vikunja
):
    """Enabled but unconfigured is not an error: nothing is called, 201."""
    set_config(VIKUNJA_DEFAULT_PROJECT_ID="")

    response = reporter.post(URL, REPORT, format="json")

    assert response.status_code == 201
    assert network.calls == []


def test_a_vikunja_task_that_is_not_created_is_a_500_with_no_label_attempt(
    reporter, network, vikunja
):
    network.answer("PUT", "/projects/7/tasks", Reply(401, {"message": "bad token"}))

    response = reporter.post(URL, REPORT, format="json")

    assert response.status_code == 500
    assert len(network.calls) == 1


def test_an_unreachable_vikunja_is_a_500(reporter, network, vikunja):
    network.answer("PUT", "/projects/7/tasks", requests.ConnectionError("no route"))

    response = reporter.post(URL, REPORT, format="json")

    assert response.status_code == 500


def test_a_label_that_cannot_be_added_does_not_fail_the_report(
    reporter, network, vikunja, monkeypatch
):
    """The task exists and is linked; only the tag is missing, so it is a 201."""
    network.answer("PUT", "/projects/7/tasks", Reply(201, {"id": 55}))
    network.answer("PUT", "/tasks/55/labels", Reply(404, {"message": "no label"}))

    warnings = []
    monkeypatch.setattr(
        "api_member_tools.views.logger.warning",
        lambda message, *args: warnings.append(message),
    )

    response = reporter.post(URL, REPORT, format="json")

    assert response.status_code == 201
    assert warnings == ["Failed to add label to Vikunja task 55: %s"]


def test_a_label_request_that_raises_does_not_fail_the_report(
    reporter, network, vikunja
):
    network.answer("PUT", "/projects/7/tasks", Reply(201, {"id": 55}))
    network.answer("PUT", "/tasks/55/labels", requests.ConnectionError("no route"))

    assert reporter.post(URL, REPORT, format="json").status_code == 201


@pytest.mark.parametrize(
    "setting, value",
    [
        ("VIKUNJA_DEFAULT_LABEL_ID", ""),
        ("VIKUNJA_DEFAULT_LABEL_ID", "urgent"),
        ("VIKUNJA_DEFAULT_PROJECT_ID", "inbox"),
    ],
)
def test_a_blank_or_non_numeric_vikunja_id_raises_instead_of_failing_cleanly(
    reporter, network, vikunja, set_config, setting, value
):
    """DEFECT, pinned: ``int(...)`` runs above the ``try``, on settings text.

    The label is documented as ``[optional]`` and ships as ``""``, so a space
    that fills in the project and leaves the label blank -- exactly what the
    admin page invites -- makes every report a ``ValueError`` and a 500, before
    Vikunja is contacted and before the Trello, email and Discord legs get a
    turn. A non-numeric project id does the same.
    """
    set_config(**{setting: value})

    with pytest.raises(ValueError):
        reporter.post(URL, REPORT, format="json")

    assert network.calls == []


def test_the_vikunja_and_trello_requests_have_no_timeout(
    reporter, network, vikunja, set_config
):
    """DEFECT, pinned: neither integration passes ``timeout=``.

    ``requests`` waits forever by default, so a Vikunja or Trello that accepts
    the connection and never answers holds the member's request, and a worker,
    until the proxy gives up.
    """
    vikunja_ok(network)
    reporter.post(URL, REPORT, format="json")

    assert all("timeout" not in kwargs for _method, _url, kwargs in network.calls)


# --------------------------------------------------------------------------
# Trello
# --------------------------------------------------------------------------


def test_a_report_becomes_a_trello_card(reporter, network, trello):
    network.answer("POST", "/1/cards", Reply(200, {"shortUrl": "https://trello.com/c/x"}))

    response = reporter.post(URL, REPORT, format="json")

    assert response.status_code == 201
    ((method, url, kwargs),) = network.calls
    assert (method, url) == ("POST", "https://api.trello.com/1/cards")
    assert kwargs["params"] == {
        "name": "Lathe is broken",
        "desc": "Test Member1: Grinding noise",
        "pos": "top",
        "idList": "list-1",
        "keepFromSource": "all",
        "key": "trello-key",
        "token": "trello-token",
    }


def test_trello_refusing_the_card_is_a_500(reporter, network, trello):
    network.answer("POST", "/1/cards", Reply(401, {"message": "unauthorized"}))

    assert reporter.post(URL, REPORT, format="json").status_code == 500


def test_an_unreachable_trello_is_a_500(reporter, network, trello):
    network.answer("POST", "/1/cards", requests.ConnectionError("no route"))

    assert reporter.post(URL, REPORT, format="json").status_code == 500


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------


@pytest.fixture
def discord_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "api_member_tools.views.discord.post_reported_issue_to_discord",
        lambda *args: calls.append(args),
    )
    return calls


def test_discord_gets_the_report_with_links_to_the_task_and_card(
    reporter, network, vikunja, trello, set_config, discord_calls
):
    """Pinned as behaviour: Discord is sent the description *without* the name.

    Vikunja and Trello get ``"Name: text"``; Discord receives the bare text and
    the name as a separate argument, which its message puts in the headline.
    """
    set_config(REPORT_ISSUE_ENABLE_DISCORD=True, REPORT_ISSUE_ENABLE_VIKUNJA=True)
    vikunja_ok(network)
    network.answer("POST", "/1/cards", Reply(200, {"shortUrl": "https://trello.com/c/x"}))

    assert reporter.post(URL, REPORT, format="json").status_code == 201

    assert discord_calls == [
        (
            "Test Member1",
            "Lathe is broken",
            "Grinding noise",
            "https://tasks.example.org/tasks/55",
            "https://trello.com/c/x",
        )
    ]


def test_discord_is_posted_to_even_when_the_other_legs_failed(
    reporter, network, vikunja, set_config, discord_calls
):
    set_config(REPORT_ISSUE_ENABLE_DISCORD=True)
    network.answer("PUT", "/projects/7/tasks", Reply(500, {}))

    response = reporter.post(URL, REPORT, format="json")

    assert response.status_code == 500
    assert len(discord_calls) == 1
    assert discord_calls[0][3:] == (None, None)


def test_the_discord_message_carries_the_links(
    reporter, network, vikunja, set_config, monkeypatch
):
    """Through the real ``services.discord`` function, with its POST captured."""
    set_config(
        REPORT_ISSUE_ENABLE_DISCORD=True,
        ENABLE_DISCORD_INTEGRATION=True,
        DISCORD_REPORT_ISSUE_WEBHOOK="https://discord.example/hook",
    )
    vikunja_ok(network)
    posted = []
    monkeypatch.setattr(
        requests, "post", lambda url, json=None, **kw: posted.append((url, json))
    )

    assert reporter.post(URL, REPORT, format="json").status_code == 201

    ((url, body),) = posted
    assert url == "https://discord.example/hook"
    assert body["content"] == "Test Member1 just reported a new issue!"
    assert body["embeds"][0]["title"] == "Lathe is broken"
    assert "[View in Vikunja](https://tasks.example.org/tasks/55)" in (
        body["embeds"][0]["description"]
    )


def test_a_discord_failure_escapes_the_view_unhandled(
    reporter, network, set_config, monkeypatch
):
    """DEFECT, pinned: the Discord leg is the only one outside a ``try``.

    ``post_reported_issue_to_discord`` swallows a read timeout and nothing
    else, so a refused connection raises out of the view -- a 500 with no
    ``failed`` bookkeeping, after the email, task and card have all been
    delivered.
    """
    set_config(
        REPORT_ISSUE_ENABLE_EMAIL=False,
        REPORT_ISSUE_ENABLE_DISCORD=True,
        ENABLE_DISCORD_INTEGRATION=True,
        DISCORD_REPORT_ISSUE_WEBHOOK="https://discord.example/hook",
    )

    def _refused(*args, **kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", _refused)

    with pytest.raises(requests.ConnectionError):
        reporter.post(URL, REPORT, format="json")


# --------------------------------------------------------------------------
# Combining them
# --------------------------------------------------------------------------


def test_one_failing_leg_does_not_stop_the_others_but_makes_the_request_a_500(
    reporter, network, vikunja, trello, set_config, sent_emails
):
    """Pinned as behaviour: the report is delivered twice if the member retries.

    Vikunja is down; the Trello card and the email still go out. The member is
    told it failed, and pressing submit again duplicates both.
    """
    set_config(
        REPORT_ISSUE_ENABLE_VIKUNJA=True,
        REPORT_ISSUE_ENABLE_EMAIL=True,
        EMAIL_ADMIN="admin@example.com",
    )
    network.answer("PUT", "/projects/7/tasks", requests.ConnectionError("down"))
    network.answer("POST", "/1/cards", Reply(200, {"shortUrl": "https://trello.com/c/x"}))

    first = reporter.post(URL, REPORT, format="json")
    second = reporter.post(URL, REPORT, format="json")

    assert first.status_code == second.status_code == 500
    assert len(network.to("/1/cards")) == 2
    assert len(sent_emails) == 2


def test_an_email_that_raises_is_a_500_and_the_other_legs_still_run(
    reporter, network, trello, set_config, monkeypatch
):
    set_config(REPORT_ISSUE_ENABLE_EMAIL=True)
    network.answer("POST", "/1/cards", Reply(200, {"shortUrl": "https://trello.com/c/x"}))

    def _down(**kwargs):
        raise RuntimeError("mail provider down")

    monkeypatch.setattr("api_member_tools.views.send_email_to_admin", _down)

    response = reporter.post(URL, REPORT, format="json")

    assert response.status_code == 500
    assert len(network.to("/1/cards")) == 1
