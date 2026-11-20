"""Characterisation: the outbound integrations in ``services/``.

Everything here talks to something the portal does not control -- Twilio,
Moodle, Canvas, Discord, Slack, Postmark -- and is called from inside a request
or a device conversation that has other work to finish. So the question these
tests keep asking is the one the callers care about: when the far end is
slow, down, or answers something unexpected, does the damage stop at the
integration or reach the member or the door?

No test here makes a network call. ``requests`` is replaced by ``Net``, which
records each call and answers from a script, and Twilio and Postmark are
replaced by small fakes.
"""

import types

import pytest
import requests

pytestmark = pytest.mark.django_db


class Reply:
    def __init__(self, body=None):
        self._body = body

    def json(self):
        return self._body


class Net:
    """Records ``requests.get``/``post`` and answers from a script."""

    def __init__(self):
        self.calls = []
        self.reply = Reply({})
        self.error = None

    def _call(self, verb, url, **kwargs):
        self.calls.append((verb, url, kwargs))
        if self.error:
            raise self.error
        return self.reply

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(requests, "get", n.get)
    monkeypatch.setattr(requests, "post", n.post)
    return n


# --------------------------------------------------------------------------
# SMS
# --------------------------------------------------------------------------


class FakeTwilio:
    sent = []
    fail = None

    def __init__(self, sid, token):
        self.messages = self

    def create(self, body, from_, to):
        if FakeTwilio.fail:
            raise FakeTwilio.fail
        FakeTwilio.sent.append({"body": body, "from": from_, "to": to})


@pytest.fixture
def twilio(monkeypatch, set_config):
    FakeTwilio.sent = []
    FakeTwilio.fail = None
    monkeypatch.setattr("services.sms.Client", FakeTwilio)
    set_config(
        SMS_ENABLE=True,
        SMS_DEFAULT_COUNTRY_CODE="+61",
        SMS_SENDER_ID="MakerMatters",
        SMS_FOOTER="",
    )
    return FakeTwilio


def test_a_disabled_sms_service_sends_nothing_and_says_so_in_the_recipients_log(
    make_member, set_config
):
    from profile.models import UserEventLog
    from services.sms import SMS

    set_config(SMS_ENABLE=False)
    member = make_member(state="active", rfid="SMS-1")

    assert SMS().send_custom_notification("0400000000", "hi", None, member.user) is None

    (entry,) = UserEventLog.objects.filter(user=member.user, logtype="sms")
    assert entry.description == "Skipped sending SMS because SMS is turned off"
    assert entry.data == "hi"


def test_the_skipped_sms_log_names_the_recipient(make_member, set_config):
    """Used to name the sender in the recipient's log.

    With SMS off and both an admin (sender) and a member (recipient) given, the
    sender's log says who the message was meant for, mirroring the enabled path.
    """
    from profile.models import UserEventLog
    from services.sms import SMS

    set_config(SMS_ENABLE=False)
    sender = make_member(state="active", rfid="SMS-S")
    recipient = make_member(state="active", rfid="SMS-R")

    SMS().send_custom_notification("0400000000", "hi", sender.user, recipient.user)

    names = [
        e.description
        for e in UserEventLog.objects.filter(user=sender.user, logtype="sms")
    ]
    assert (
        f"Skipped sending SMS to {recipient.user.get_full_name()} because SMS is turned off."
        in names
    )


def test_an_enabled_sms_service_adds_the_country_code_and_footer(twilio, set_config):
    from services.sms import SMS

    set_config(SMS_FOOTER="Reply STOP to opt out")

    SMS().send_custom_notification("0400000000", "Hello")

    assert twilio.sent == [
        {
            "body": "Hello\n\nReply STOP to opt out",
            "from": "MakerMatters",
            "to": "+610400000000",
        }
    ]


def test_a_number_that_already_has_a_country_code_is_left_alone(twilio):
    from services.sms import SMS

    SMS().send_custom_notification("+46701234567", "Hej")

    assert twilio.sent[0]["to"] == "+46701234567"


def test_sending_logs_against_both_people(twilio, make_member):
    from profile.models import UserEventLog
    from services.sms import SMS

    sender = make_member(state="active", rfid="SMS-A")
    recipient = make_member(state="active", rfid="SMS-B")

    SMS().send_custom_notification("0400000000", "Hello", sender.user, recipient.user)

    assert UserEventLog.objects.get(user=recipient.user, logtype="sms").description == (
        f"Sent SMS by {sender.user.get_full_name()}: Hello"
    )
    assert UserEventLog.objects.get(user=sender.user, logtype="sms").description == (
        f"Sent SMS to {recipient.user.get_full_name()}: Hello"
    )


@pytest.mark.parametrize(
    "method, key, fragment",
    [
        ("send_inactive_swipe_alert", "inactive_swipe", "swipe was just declined"),
        ("send_locked_out_swipe_alert", "locked_out_swipe", "maintenance lockout"),
        ("send_deactivated_access", "deactivated_access", "turned off"),
        ("send_activated_access", "activated_access", "turned on"),
    ],
)
def test_each_alert_uses_the_configured_text_or_a_default(
    twilio, set_config, method, key, fragment
):
    import json

    from services.sms import SMS

    set_config(SMS_MESSAGES=json.dumps({key: "Custom text"}))
    getattr(SMS(), method)("0400000000")
    set_config(SMS_MESSAGES="{}")
    getattr(SMS(), method)("0400000000")

    assert twilio.sent[0]["body"] == "Custom text"
    assert fragment in twilio.sent[1]["body"]


@pytest.mark.parametrize(
    "number, body, message",
    [("0400000000", "", "no characters"), ("0400", "hi", "less than 10"), ("", "hi", "less than 10")],
)
def test_an_empty_message_or_short_number_raises(twilio, number, body, message):
    from services.sms import SMS

    with pytest.raises(RuntimeError, match=message):
        SMS().send_custom_notification(number, body)

    assert twilio.sent == []


def test_malformed_sms_messages_make_the_service_unconstructable(set_config):
    """Pinned as behaviour: ``SMS()`` parses the setting and re-raises."""
    from services.sms import SMS

    set_config(SMS_MESSAGES="{not json")

    with pytest.raises(ValueError):
        SMS()


def test_deactivating_a_member_with_no_phone_number_still_deactivates(
    twilio, make_member, sent_emails
):
    """Used to fail: a blank phone number blocked deactivation.

    ``phone`` is optional, and ``SMS._send`` rejects a blank number. The state
    change is now made first and the notifications are best effort, so the
    committee's button and the ``customer.subscription.deleted`` webhook work.
    """
    member = make_member(state="active", rfid="SMS-NOPHONE")
    member.phone = ""
    member.save()

    assert member.deactivate() is True

    member.refresh_from_db()
    assert member.state == "inactive"
    assert len(sent_emails) == 1


def test_activating_a_member_with_no_phone_number_still_activates(
    twilio, make_member, sent_emails
):
    member = make_member(state="inactive", rfid="SMS-NOPHONE2")
    member.phone = ""
    member.save()

    assert member.activate() is True

    member.refresh_from_db()
    assert member.state == "active"


def test_a_twilio_failure_does_not_block_deactivation(twilio, make_member, sent_emails):
    """Used to: any SMS provider outage stopped the deactivation."""
    member = make_member(state="active", rfid="SMS-DOWN")
    member.phone = "0400000000"
    member.save()
    twilio.fail = RuntimeError("Twilio is down")

    assert member.deactivate() is True

    member.refresh_from_db()
    assert member.state == "inactive"


# --------------------------------------------------------------------------
# Moodle
# --------------------------------------------------------------------------


@pytest.fixture
def moodle(set_config):
    set_config(MOODLE_API_BASE_URL="https://moodle.example", MOODLE_API_TOKEN="tok")


def test_the_moodle_url_carries_the_token_and_function(moodle):
    from services.moodle_integration import get_moodle_url

    assert get_moodle_url("core_x") == (
        "https://moodle.example/webservice/rest/server.php"
        "?wstoken=tok&moodlewsrestformat=json&wsfunction=core_x"
    )


@pytest.mark.parametrize(
    "state, name",
    [(0, "incomplete"), (1, "complete"), (2, "complete_pass"), (3, "complete_fail")],
)
def test_moodle_completion_states_have_names(state, name):
    from services.moodle_integration import get_moodle_completion_state

    assert get_moodle_completion_state(state) == name


def test_an_unknown_moodle_completion_state_raises():
    from services.moodle_integration import get_moodle_completion_state

    with pytest.raises(RuntimeError, match="Invalid completion state id: 9"):
        get_moodle_completion_state(9)


def test_course_progress_is_the_share_of_activities_completed(moodle, net):
    from services.moodle_integration import moodle_get_course_activity_completion_status

    net.reply = Reply({"statuses": [{"state": 0}, {"state": 1}, {"state": 2}]})

    result = moodle_get_course_activity_completion_status("5", "42")

    assert result == {
        "total_course_activities": 3,
        "completed_course_activities": 2,
        "incomplete_course_activities": 1,
        "percentage_completed": 66,
    }
    assert net.calls[0][1].endswith("&courseid=5&userid=42")


def test_a_failed_activity_does_not_count_as_completed(moodle, net):
    """Used to count: ``complete_fail`` was in the completed bucket, so a member
    who failed every activity scored 100%."""
    from services.moodle_integration import moodle_get_course_activity_completion_status

    net.reply = Reply({"statuses": [{"state": 3}, {"state": 3}]})

    result = moodle_get_course_activity_completion_status("5", "42")

    assert result["percentage_completed"] == 0
    assert result["completed_course_activities"] == 0
    assert result["incomplete_course_activities"] == 2


def test_a_mix_of_passed_and_failed_activities_scores_only_the_passes(moodle, net):
    from services.moodle_integration import moodle_get_course_activity_completion_status

    net.reply = Reply({"statuses": [{"state": 1}, {"state": 2}, {"state": 3}, {"state": 0}]})

    assert moodle_get_course_activity_completion_status("5", "42")[
        "percentage_completed"
    ] == 50


def test_a_course_with_no_activities_is_zero_percent_complete(moodle, net):
    """Used to raise ZeroDivisionError."""
    from services.moodle_integration import moodle_get_course_activity_completion_status

    net.reply = Reply({"statuses": []})

    result = moodle_get_course_activity_completion_status("5", "42")

    assert result["total_course_activities"] == 0
    assert result["percentage_completed"] == 0


def test_a_moodle_error_document_is_reported_with_moodles_message(moodle, net):
    """Used to be ``TypeError: len(None)``. Moodle reports problems as
    ``{"exception": ...}`` with a 200, so ``statuses`` is absent."""
    from services.moodle_integration import moodle_get_course_activity_completion_status

    net.reply = Reply({"exception": "webservice_access_exception", "message": "no"})

    with pytest.raises(RuntimeError, match="no"):
        moodle_get_course_activity_completion_status("5", "42")


def test_looking_a_user_up_by_email_returns_the_fields_the_portal_uses(moodle, net):
    from services.moodle_integration import moodle_get_user_from_email

    row = {
        "id": 7,
        "email": "a@example.com",
        "username": "a",
        "firstname": "A",
        "lastname": "B",
        "fullname": "A B",
        "extra": "ignored",
    }
    net.reply = Reply([row])

    user = moodle_get_user_from_email("a@example.com")

    assert user == {k: v for k, v in row.items() if k != "extra"}


@pytest.mark.parametrize(
    "rows, message", [([], "No users found"), ([{}, {}], "Multiple users found")]
)
def test_an_ambiguous_or_missing_moodle_user_raises(moodle, net, rows, message):
    from services.moodle_integration import moodle_get_user_from_email

    net.reply = Reply(rows)

    with pytest.raises(RuntimeError, match=message):
        moodle_get_user_from_email("a@example.com")


def test_an_email_is_passed_to_moodle_as_an_encoded_parameter(moodle, net):
    """Used to be concatenated into the query string, so ``a+b@example.com``
    was looked up as ``a b@example.com`` and an ``&`` injected a parameter."""
    from services.moodle_integration import moodle_get_user_from_email

    net.reply = Reply([])

    with pytest.raises(RuntimeError):
        moodle_get_user_from_email("a+b@example.com&x=1")

    kwargs = net.calls[0][2]
    assert kwargs["params"] == {"field": "email", "values[0]": "a+b@example.com&x=1"}
    assert "a+b" not in net.calls[0][1]


def test_moodle_requests_have_a_timeout(moodle, net, settings):
    """Used to pass no ``timeout=``, so a stalled Moodle held the request open."""
    from services.moodle_integration import (
        moodle_get_course_activity_completion_status,
        moodle_get_user_from_email,
    )

    net.reply = Reply({"statuses": [{"state": 1}]})
    moodle_get_course_activity_completion_status("5", "42")
    net.reply = Reply([{"id": 1, "email": "", "username": "", "firstname": "", "lastname": "", "fullname": ""}])
    moodle_get_user_from_email("a@example.com")

    assert all(
        kwargs.get("timeout") == settings.REQUEST_TIMEOUT
        for _verb, _url, kwargs in net.calls
    )


# --------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------


def canvas_course(*students):
    return Reply({"data": {"course": {"enrollmentsConnection": {"nodes": list(students)}}}})


def student(email, score):
    return {"user": {"email": email}, "grades": {"finalScore": score}}


@pytest.fixture
def canvas(set_config, net):
    from services.canvas import Canvas

    set_config(CANVAS_API_TOKEN="canvas-token")
    return Canvas()


def test_course_scores_are_keyed_by_lowercased_email_and_skip_blank_ones(canvas, net):
    net.reply = canvas_course(
        student("Ann@Example.com", 91.5), student(None, 50), student("", 40)
    )

    assert canvas.get_course_scores("12") == {"ann@example.com": 91.5}


def test_the_graphql_query_names_the_course_and_sends_the_token(canvas, net):
    net.reply = canvas_course()

    canvas.get_course_scores("12")

    verb, url, kwargs = net.calls[0]
    assert (verb, url) == ("post", "https://canvas.instructure.com/api/graphql/")
    assert kwargs["headers"] == {"Authorization": "Bearer canvas-token"}
    assert 'course(id: "12")' in kwargs["data"]["query"]


def test_a_students_score_is_found_case_insensitively_and_none_if_absent(canvas, net):
    net.reply = canvas_course(student("ann@example.com", 77))

    assert canvas.get_student_score_for_course("12", "ANN@example.com") == 77
    assert canvas.get_student_score_for_course("12", "bob@example.com") is None


def test_an_answer_with_no_data_means_no_scores(canvas, net):
    net.reply = Reply({"errors": [{"message": "unauthorised"}]})

    assert canvas.get_course_scores("12") == {}


def test_a_course_that_does_not_exist_has_no_scores(canvas, net):
    """Used to be ``TypeError``: ``data.course`` is ``null`` for an unknown id."""
    net.reply = Reply({"data": {"course": None}})

    assert canvas.get_course_scores("999") == {}


def test_an_enrolment_with_no_user_is_skipped(canvas, net):
    """Used to fail the check for the whole course with ``AttributeError``."""
    net.reply = canvas_course(
        {"user": None, "grades": {"finalScore": 1}},
        {"user": {"email": "A@B.c"}, "grades": {"finalScore": 80}},
    )

    assert canvas.get_course_scores("12") == {"a@b.c": 80}


def test_an_enrolment_with_no_grades_scores_none(canvas, net):
    net.reply = canvas_course({"user": {"email": "a@b.c"}, "grades": None})

    assert canvas.get_course_scores("12") == {"a@b.c": None}


def test_canvas_requests_have_a_timeout(canvas, net, settings):
    """Used to pass no ``timeout=``."""
    net.reply = canvas_course()
    canvas.get_course_scores("12")

    assert all(
        kwargs.get("timeout") == settings.REQUEST_TIMEOUT
        for _verb, _url, kwargs in net.calls
    )


# --------------------------------------------------------------------------
# Discord and Slack
# --------------------------------------------------------------------------


@pytest.fixture
def chat(set_config, net):
    set_config(
        ENABLE_DISCORD_INTEGRATION=True,
        ENABLE_SLACK_INTEGRATION=True,
        DISCORD_DOOR_WEBHOOK="https://discord.example/door",
        DISCORD_INTERLOCK_WEBHOOK="https://discord.example/interlock",
        DISCORD_MEMBERBUCKS_PURCHASE_WEBHOOK="https://discord.example/buy",
        SLACK_DOOR_WEBHOOK="https://slack.example/door",
        SLACK_INTERLOCK_WEBHOOK="https://slack.example/interlock",
        SITE_URL="https://portal.example",
    )
    return net


def discord_text(net):
    ((_verb, _url, kwargs),) = net.calls
    return kwargs["json"]["embeds"][0]["description"]


def slack_text(net):
    ((_verb, _url, kwargs),) = net.calls
    return kwargs["json"]["text"]


DOOR_STATUSES = [
    (True, "successfully"),
    ("not_signed_in", "aren't signed into site"),
    ("locked_out", "maintenance lockout"),
    (False, "**rejected**"),
]


@pytest.mark.parametrize("status, fragment", DOOR_STATUSES)
def test_a_door_swipe_is_announced_on_discord(chat, status, fragment):
    from services.discord import post_door_swipe_to_discord

    assert post_door_swipe_to_discord("Ann", "Front", status) is True

    assert fragment in discord_text(chat)
    assert chat.calls[0][1] == "https://discord.example/door"
    assert chat.calls[0][2]["timeout"]


@pytest.mark.parametrize("status, fragment", DOOR_STATUSES)
def test_a_door_swipe_is_announced_on_slack(chat, status, fragment):
    from services.slack import post_door_swipe_to_slack

    assert post_door_swipe_to_slack("Ann", "Front", status) is True

    assert fragment in slack_text(chat)
    assert chat.calls[0][1] == "https://slack.example/door"


INTERLOCK_TYPES = ["activated", "rejected", "left_on", "deactivated", "locked_out", "not_signed_in"]


@pytest.mark.parametrize("kind", INTERLOCK_TYPES)
def test_an_interlock_event_is_announced_on_discord_and_slack(chat, kind):
    from services.discord import post_interlock_swipe_to_discord
    from services.slack import post_interlock_swipe_to_slack

    post_interlock_swipe_to_discord("Ann", "Lathe", kind, time="5 mins")
    post_interlock_swipe_to_slack("Ann", "Lathe", kind, time="5 mins")

    discord_call, slack_call = chat.calls
    assert discord_call[1] == "https://discord.example/interlock"
    assert slack_call[1] == "https://slack.example/interlock"
    assert "Ann" in discord_call[2]["json"]["embeds"][0]["description"]
    assert "Ann" in slack_call[2]["json"]["text"]


def test_an_unrecognised_interlock_event_posts_nothing(chat):
    """Used to post ``{"description": "", "embeds": []}`` / ``{}``, which the
    services reject."""
    from services.discord import post_interlock_swipe_to_discord
    from services.slack import post_interlock_swipe_to_slack

    assert post_interlock_swipe_to_discord("Ann", "Lathe", "exploded") is True
    assert post_interlock_swipe_to_slack("Ann", "Lathe", "exploded") is True

    assert chat.calls == []


def test_discords_interlock_post_returns_true_when_it_succeeds(chat):
    """Used to return ``None``, unlike every other notifier."""
    from services.discord import post_interlock_swipe_to_discord

    assert post_interlock_swipe_to_discord("Ann", "Lathe", "activated") is True


def test_the_smaller_announcements(chat):
    from services.discord import (
        post_door_bump_to_discord,
        post_kiosk_swipe_to_discord,
        post_purchase_to_discord,
    )
    from services.slack import post_door_bump_to_slack, post_kiosk_swipe_to_slack

    post_door_bump_to_discord("Ann", "Front")
    post_door_bump_to_slack("Ann", "Front")
    post_purchase_to_discord("Ann bought a Mars bar")
    post_kiosk_swipe_to_discord("Ann", False)
    post_kiosk_swipe_to_slack("Ann", True)

    texts = [
        c[2]["json"]["embeds"][0]["description"] if "embeds" in c[2]["json"] else c[2]["json"]["text"]
        for c in chat.calls
    ]
    assert texts == [
        ":unlock: Ann just bumped at Front.",
        ":unlock: Ann just bumped Front.",
        ":coin: Ann bought a Mars bar",
        ":book: Ann just signed out at a kiosk.",
        ":book: Ann just signed in at a kiosk.",
    ]
    assert chat.calls[2][1] == "https://discord.example/buy"


def test_nothing_is_posted_when_the_integration_is_off_or_has_no_webhook(
    chat, set_config
):
    from services.discord import post_door_swipe_to_discord
    from services.slack import post_door_swipe_to_slack

    set_config(ENABLE_DISCORD_INTEGRATION=False, ENABLE_SLACK_INTEGRATION=True, SLACK_DOOR_WEBHOOK="")

    assert post_door_swipe_to_discord("Ann", "Front", True) is True
    assert post_door_swipe_to_slack("Ann", "Front", True) is True
    assert chat.calls == []


def test_a_read_timeout_is_swallowed(chat):
    from services.discord import post_door_swipe_to_discord
    from services.slack import post_door_swipe_to_slack

    chat.error = requests.exceptions.ReadTimeout("slow")

    assert post_door_swipe_to_discord("Ann", "Front", True) is True
    assert post_door_swipe_to_slack("Ann", "Front", True) is True


@pytest.mark.parametrize(
    "error", [requests.exceptions.ConnectionError("refused"), requests.exceptions.ConnectTimeout("no route")]
)
def test_any_network_error_is_swallowed_by_the_notifier(chat, error):
    """Used to escape: only ``ReadTimeout`` was caught.

    The notifiers are called in the middle of other work, so a chat service that
    is down must not raise out of them.
    """
    from services.discord import post_door_swipe_to_discord
    from services.slack import post_door_swipe_to_slack

    chat.error = error

    assert post_door_swipe_to_discord("Ann", "Front", True) is True
    assert post_door_swipe_to_slack("Ann", "Front", True) is True


def test_a_discord_outage_does_not_break_logging_a_door_swipe(
    chat, make_door, make_member
):
    """Used to raise into the door's WebSocket consumer after the ``DoorLog``
    row had been written, dropping the door's connection."""
    from access.models import DoorLog

    door = make_door(serial="chat-door", post_to_discord=True)
    member = make_member(state="active", rfid="CHAT-DOOR")
    chat.error = requests.exceptions.ConnectionError("refused")

    door.log_access(member.user.id, success=True)

    assert DoorLog.objects.filter(door=door, user=member.user).count() == 1


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


def test_message_text_is_html_escaped_but_br_markers_become_line_breaks(
    sent_emails, set_config
):
    from services.emails import send_single_email

    set_config(POSTMARK_API_KEY="key")

    assert send_single_email(
        "to@example.com",
        "Subject",
        {"title": "T", "message": "<script>x</script>~br~second line"},
    ) is True

    html = sent_emails[0]["HtmlBody"]
    assert "&lt;script&gt;x&lt;/script&gt;<br>second line" in html
    assert "<script>x" not in html


def test_without_a_postmark_key_nothing_is_sent_and_it_reports_that(
    sent_emails, set_config, make_member
):
    """Used to return ``True`` for an email it did not send, so callers that
    test the result (the issue reporter) could never see the "not configured"
    case as a failure. It now returns ``False``, and still notes it on the
    member's log."""
    from profile.models import UserEventLog
    from services.emails import send_single_email

    set_config(POSTMARK_API_KEY="")
    member = make_member(state="active", rfid="MAIL-1")

    assert send_single_email("to@example.com", "Hi", {"message": "x"}, user=member.user) is False

    assert sent_emails == []
    entry = UserEventLog.objects.get(user=member.user, logtype="email")
    assert entry.description == "Email NOT sent due to configuration issue: Hi"


def _postmark_raising(monkeypatch, code):
    from postmarker.core import ClientError

    class Emails:
        @staticmethod
        def send(**kwargs):
            raise ClientError("provider said no", error_code=code)

    monkeypatch.setattr(
        "services.emails.PostmarkClient",
        lambda **kwargs: types.SimpleNamespace(emails=Emails()),
    )


def test_an_inactive_recipient_is_logged_and_reported_as_not_sent(
    monkeypatch, set_config, make_member
):
    """Postmark's 406 means the address bounced before and is suppressed.

    The email is not sent, the member's log says so, and the function returns
    ``False`` (it used to say ``True``, and also logged "Sent email" for it).
    Without a ``user`` there is no trace at all beyond the logger.
    """
    from profile.models import UserEventLog
    from services.emails import send_single_email

    set_config(POSTMARK_API_KEY="key")
    _postmark_raising(monkeypatch, 406)
    member = make_member(state="active", rfid="MAIL-2")

    assert send_single_email("to@example.com", "Hi", {"message": "x"}, user=member.user) is False
    assert send_single_email("to@example.com", "Hi", {"message": "x"}) is False

    assert UserEventLog.objects.filter(
        user=member.user, description__startswith="Email NOT sent because recipient is INACTIVE"
    ).count() == 1
    assert not UserEventLog.objects.filter(
        user=member.user, description__startswith="Sent email"
    ).exists()


def test_any_other_postmark_error_is_raised(monkeypatch, set_config):
    from postmarker.core import ClientError
    from services.emails import send_single_email

    set_config(POSTMARK_API_KEY="key")
    _postmark_raising(monkeypatch, 300)

    with pytest.raises(ClientError):
        send_single_email("to@example.com", "Hi", {"message": "x"})


def test_a_sent_email_is_logged_against_the_user(sent_emails, set_config, make_member):
    from profile.models import UserEventLog
    from services.emails import send_email_to_admin

    set_config(POSTMARK_API_KEY="key", EMAIL_ADMIN="admin@example.com")
    member = make_member(state="active", rfid="MAIL-3")

    send_email_to_admin("Subj", {"message": "m"}, reply_to="r@example.com", user=member.user)

    assert sent_emails[0]["To"] == "admin@example.com"
    assert sent_emails[0]["ReplyTo"] == "r@example.com"
    assert UserEventLog.objects.get(user=member.user, logtype="email").description == (
        "Sent email with subject: Subj"
    )


# --------------------------------------------------------------------------
# The remaining notifiers
# --------------------------------------------------------------------------


def test_a_reported_issue_is_posted_with_links_to_the_task_and_card(chat, set_config):
    from services.discord import post_reported_issue_to_discord

    set_config(DISCORD_REPORT_ISSUE_WEBHOOK="https://discord.example/issues")

    assert post_reported_issue_to_discord(
        "Ann", "Lathe", "Grinds", "https://v.example/t/1", "https://trello.com/c/x"
    ) is True

    ((_verb, url, kwargs),) = chat.calls
    assert url == "https://discord.example/issues"
    assert kwargs["json"]["content"] == "Ann just reported a new issue!"
    embed = kwargs["json"]["embeds"][0]
    assert embed["title"] == "Lathe"
    assert embed["description"] == (
        "Grinds\n\n[View in Vikunja](https://v.example/t/1)\n\n[View in Trello](https://trello.com/c/x)"
    )


def test_a_reported_issue_without_links_is_posted_as_typed(chat, set_config):
    from services.discord import post_reported_issue_to_discord

    set_config(DISCORD_REPORT_ISSUE_WEBHOOK="https://discord.example/issues")

    post_reported_issue_to_discord("Ann", "Lathe", "Grinds")

    assert chat.calls[0][2]["json"]["embeds"][0]["description"] == "Grinds"


def test_every_notifier_swallows_a_read_timeout_and_still_reports_success(
    chat, set_config
):
    from services import discord, slack

    set_config(DISCORD_REPORT_ISSUE_WEBHOOK="https://discord.example/issues")
    chat.error = requests.exceptions.ReadTimeout("slow")

    results = [
        discord.post_door_bump_to_discord("Ann", "Front"),
        discord.post_kiosk_swipe_to_discord("Ann", True),
        discord.post_purchase_to_discord("bought"),
        discord.post_reported_issue_to_discord("Ann", "T", "D"),
        discord.post_interlock_swipe_to_discord("Ann", "Lathe", "activated"),
        slack.post_door_bump_to_slack("Ann", "Front"),
        slack.post_kiosk_swipe_to_slack("Ann", True),
        slack.post_interlock_swipe_to_slack("Ann", "Lathe", "activated"),
    ]

    assert results == [True] * 8
    assert len(chat.calls) == 8
