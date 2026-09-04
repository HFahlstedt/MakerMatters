"""Characterisation: the five member-facing endpoints under ``/api/tools/``.

``api_member_tools`` is what the member portal's own pages call: the recent
swipe feed, the "last seen" board, the upcoming meetings list, the member
directory, and the report-an-issue form.

Nothing here is admin-gated. Every endpoint is readable by any logged-in
member, which is the intent -- these are the shared-noticeboard pages of the
portal -- but it does mean the directory and the swipe feed hand a member's
movements and full name to every other member, so the authentication check
on each is worth pinning in its own right.

``Lastseen`` keeps its queryset as a class attribute, which is fine because
its filter is a constant. ``MeetingList`` used to do the same with a filter
containing ``timezone.now()``, so its cutoff was fixed when the module was
imported; it builds the queryset per request now. See
``test_the_upcoming_cutoff_is_recomputed_on_every_request``.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = pytest.mark.django_db


TOOLS_ENDPOINTS = [
    "/api/tools/swipes/",
    "/api/tools/lastseen/",
    "/api/tools/meetings/",
    "/api/tools/members/",
]

DOOR_SWIPE_FIELDS = {"name", "date", "user"}
INTERLOCK_SWIPE_FIELDS = {
    "name",
    "sessionStart",
    "sessionEnd",
    "sessionComplete",
    "userOn",
    "userOff",
}


def cutoff_of(queryset):
    """The datetime on the right of the queryset's single WHERE condition."""
    (condition,) = queryset.query.where.children
    return condition.rhs


@pytest.fixture
def make_meeting(db):
    from api_meeting.models import Meeting

    def _make(date=None, type="general", **kwargs):
        return Meeting.objects.create(
            date=date or (timezone.now() + timedelta(days=7)), type=type, **kwargs
        )

    return _make


@pytest.fixture
def integrations_off(set_config):
    """Every issue-reporting integration disabled.

    Only ``REPORT_ISSUE_ENABLE_EMAIL`` is on by default, so this leaves the
    endpoint with nothing to do and isolates its own logic.
    """
    set_config(
        REPORT_ISSUE_ENABLE_EMAIL=False,
        REPORT_ISSUE_ENABLE_DISCORD=False,
        REPORT_ISSUE_ENABLE_VIKUNJA=False,
        REPORT_ISSUE_ENABLE_TRELLO=False,
    )


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", TOOLS_ENDPOINTS)
def test_an_anonymous_caller_is_refused(api_client, path):
    assert api_client.get(path).status_code == 401


def test_an_anonymous_caller_cannot_report_an_issue(api_client):
    assert api_client.post("/api/tools/issue/", {}, format="json").status_code == 401


def test_the_member_directory_relies_on_the_project_wide_default(api_client):
    """``Members`` is the one class here that declares no permission_classes.

    It is closed only because ``DEFAULT_PERMISSION_CLASSES`` in settings.py
    is ``IsAuthenticated``. Loosening that default -- or moving this view to
    a project that does not set it -- would publish every active member's
    full name without any edit to this file.
    """
    from api_member_tools.views import Members

    # APIView supplies the attribute; what matters is that this class does not
    # declare one of its own, unlike the other four in the module.
    assert "permission_classes" not in Members.__dict__
    assert api_client.get("/api/tools/members/").status_code == 401


# --------------------------------------------------------------------------
# /api/tools/swipes/
# --------------------------------------------------------------------------


def test_the_swipe_feed_reports_door_and_interlock_activity(
    as_member, make_member, make_door, make_interlock
):
    from access.models import DoorLog, InterlockLog

    member = make_member(state="active", rfid="SWIPE-1")
    door = make_door(serial="swipe-door")
    interlock = make_interlock(serial="swipe-int")
    DoorLog.objects.create(user=member.user, door=door)
    InterlockLog.objects.create(interlock=interlock, user_started=member.user)

    body = as_member(member).get("/api/tools/swipes/").data

    assert set(body) == {"doors", "interlocks"}
    assert set(body["doors"][0]) == DOOR_SWIPE_FIELDS
    assert body["doors"][0]["name"] == door.name
    assert body["doors"][0]["user"] == "Test Member1"
    assert set(body["interlocks"][0]) == INTERLOCK_SWIPE_FIELDS
    assert body["interlocks"][0]["userOn"] == "Test Member1"


def test_an_open_interlock_session_reports_itself_incomplete(
    as_member, make_member, make_interlock
):
    """A session in progress has no end date and nobody who ended it."""
    from access.models import InterlockLog

    member = make_member(state="active", rfid="SWIPE-2")
    InterlockLog.objects.create(
        interlock=make_interlock(serial="swipe-open"), user_started=member.user
    )

    (session,) = as_member(member).get("/api/tools/swipes/").data["interlocks"]

    assert session["sessionComplete"] is False
    assert session["sessionEnd"] is None
    assert session["userOff"] is None


def test_a_finished_interlock_session_names_whoever_ended_it(
    as_member, make_member, make_interlock
):
    from access.models import InterlockLog

    starter = make_member(state="active", rfid="SWIPE-3")
    finisher = make_member(state="active", rfid="SWIPE-4")
    InterlockLog.objects.create(
        interlock=make_interlock(serial="swipe-closed"),
        user_started=starter.user,
        user_ended=finisher.user,
        date_ended=timezone.now(),
    )

    (session,) = as_member(starter).get("/api/tools/swipes/").data["interlocks"]

    assert session["sessionComplete"] is True
    assert session["userOn"] == "Test Member1"
    assert session["userOff"] == "Test Member2"


def test_the_swipe_feed_is_newest_first_and_capped_at_300(
    as_member, make_member, make_door
):
    from access.models import DoorLog

    member = make_member(state="active", rfid="SWIPE-5")
    door = make_door(serial="swipe-many")
    start = timezone.now() - timedelta(days=1)
    DoorLog.objects.bulk_create(
        [
            DoorLog(user=member.user, door=door, date=start + timedelta(minutes=i))
            for i in range(301)
        ]
    )

    doors = as_member(member).get("/api/tools/swipes/").data["doors"]

    assert len(doors) == 300
    assert doors[0]["date"] > doors[-1]["date"]
    # The oldest of the 301 is the one dropped.
    assert doors[-1]["date"] == start + timedelta(minutes=1)


def test_the_swipe_feed_asks_the_database_for_300_rows(
    as_member, make_member, make_door
):
    """The 300 cap used to be applied in Python, after loading everything.

    ``DoorLog.objects.all().order_by("date")[::-1][:300]`` reads like a
    limited query and is not one: Django will not push a negative slice step
    into SQL, so ``[::-1]`` evaluated the queryset in full -- every swipe ever
    recorded -- built a Python list, reversed it, and only then took 300. The
    same held for ``InterlockLog``, so a space running for years loaded its
    entire access history into memory on every request to this endpoint.

    Asserting on the emitted SQL rather than the response is the point: the
    payload was always correct, which is why nothing else caught this.
    """
    member = make_member(state="active", rfid="SWIPE-6")
    door = make_door(serial="swipe-sql")
    from access.models import DoorLog

    DoorLog.objects.create(user=member.user, door=door)

    with CaptureQueriesContext(connection) as queries:
        as_member(member).get("/api/tools/swipes/")

    log_queries = [
        q["sql"]
        for q in queries.captured_queries
        if "access_doorlog" in q["sql"] or "access_interlocklog" in q["sql"]
    ]
    assert len(log_queries) == 2, "expected one query per log table"
    assert all("LIMIT 300" in sql.upper() for sql in log_queries)


# --------------------------------------------------------------------------
# /api/tools/lastseen/
# --------------------------------------------------------------------------


def test_last_seen_lists_active_members_newest_first(as_member, make_member):
    caller = make_member(state="active", rfid="SEEN-1")
    caller.last_seen = timezone.now() - timedelta(days=2)
    caller.save()
    recent = make_member(state="active", rfid="SEEN-2")
    recent.last_seen = timezone.now()
    recent.save()

    body = as_member(caller).get("/api/tools/lastseen/").data

    assert [entry["user"] for entry in body] == ["Test Member2", "Test Member1"]
    assert set(body[0]) == {"id", "user", "never", "date"}
    assert body[0]["never"] is False


def test_a_member_who_has_never_swiped_carries_no_date_at_all(as_member, make_member):
    """The two shapes differ by a key, not by a null.

    A member who has never been seen gets ``{"id", "user", "never"}`` -- the
    "date" key is absent rather than None, so a client reading it has to
    check ``never`` first.
    """
    caller = make_member(state="active", rfid="SEEN-3")

    (entry,) = as_member(caller).get("/api/tools/lastseen/").data

    assert set(entry) == {"id", "user", "never"}
    assert entry["never"] is True


def test_last_seen_reports_the_profile_id_not_the_user_id(as_member, make_member):
    """``member`` here is a ``Profile``, so ``member.id`` is its primary key.

    The two happen to coincide in a fresh database, which is exactly what
    would let a rewrite swap one for the other unnoticed, so a divergent
    pair is forced first.
    """
    from profile.models import User

    caller = make_member(state="active", rfid="SEEN-4")
    # A user with no profile pushes the user ids one ahead of the profile ids.
    User.objects.create(email="orphan@example.com")
    subject = make_member(state="active", rfid="SEEN-5")
    assert subject.id != subject.user.id

    body = as_member(caller).get("/api/tools/lastseen/").data
    entry = next(e for e in body if e["user"] == subject.get_full_name())

    assert entry["id"] == subject.id


@pytest.mark.parametrize("state", ["noob", "inactive", "accountonly"])
def test_last_seen_excludes_everyone_who_is_not_active(as_member, make_member, state):
    caller = make_member(state="active", rfid=f"SEEN-A-{state}")
    make_member(state=state, rfid=f"SEEN-B-{state}")

    body = as_member(caller).get("/api/tools/lastseen/").data

    assert [entry["user"] for entry in body] == ["Test Member1"]


# --------------------------------------------------------------------------
# /api/tools/meetings/
# --------------------------------------------------------------------------


def test_an_upcoming_meeting_is_listed_with_its_display_name(
    as_member, make_member, make_meeting
):
    member = make_member(state="active", rfid="MEET-1")
    meeting = make_meeting(date=timezone.now() + timedelta(days=3), type="agm")

    (entry,) = as_member(member).get("/api/tools/meetings/").data

    assert set(entry) == {"id", "name", "date"}
    assert entry["id"] == meeting.id
    # The display half of the choices tuple, not the stored "agm".
    assert entry["name"] == "Annual General"


def test_a_meeting_date_is_returned_preformatted_in_the_server_locale(
    as_member, make_member, make_meeting
):
    """Unlike every other endpoint here, this one does not return a datetime.

    ``strftime("%x %X")`` renders the server's locale and timezone into a
    string, so a client cannot reformat it and cannot read it as a date.
    """
    member = make_member(state="active", rfid="MEET-2")
    make_meeting(date=timezone.now() + timedelta(days=3))

    (entry,) = as_member(member).get("/api/tools/meetings/").data

    assert isinstance(entry["date"], str)
    assert entry["date"].count("/") == 2 and entry["date"].count(":") == 2


def test_every_member_sees_every_meeting_regardless_of_entitlement(
    as_member, make_member, make_meeting
):
    """The docstring claims otherwise, and there is no filtering to match it.

    ``MeetingList`` is documented as returning "meetings that a member is
    entitled to vote at", but the queryset is filtered on date alone. A brand
    new member in the "noob" state sees the same list as everyone else. That
    may well be intended -- meeting dates are hardly secret -- but the
    docstring describes a rule the code does not implement.
    """
    noob = make_member(state="noob", rfid="MEET-3")
    make_meeting(date=timezone.now() + timedelta(days=3))

    assert len(as_member(noob).get("/api/tools/meetings/").data) == 1


def test_the_upcoming_cutoff_is_recomputed_on_every_request(db):
    """The cutoff used to be frozen when the module was imported.

    ``queryset = Meeting.objects.filter(date__gt=timezone.now())`` sat in the
    class body, so ``now()`` ran exactly once -- at process start, in
    production. ``self.queryset.all()`` did re-run the SQL per request, which
    is precisely what made it look correct, but the timestamp baked into the
    WHERE clause never moved. "Upcoming" meant "after the server last
    restarted", and the two drifted further apart the longer the process
    stayed up.

    That is invisible to any test that only inspects one response, because a
    freshly started process is momentarily right. What has to be asserted is
    that the cutoff advances between two builds of the queryset.
    """
    from api_member_tools.views import MeetingList

    view = MeetingList()
    first = cutoff_of(view.get_queryset())
    second = cutoff_of(view.get_queryset())

    assert second > first


def test_a_meeting_that_has_already_happened_is_not_listed(
    as_member, make_member, make_meeting
):
    member = make_member(state="active", rfid="MEET-4")
    make_meeting(date=timezone.now() - timedelta(minutes=30))
    upcoming = make_meeting(date=timezone.now() + timedelta(days=1))

    body = as_member(member).get("/api/tools/meetings/").data

    assert [entry["id"] for entry in body] == [upcoming.id]


# --------------------------------------------------------------------------
# /api/tools/members/
# --------------------------------------------------------------------------


def test_the_directory_lists_active_members_only(as_member, make_member):
    caller = make_member(state="active", rfid="DIR-1")
    make_member(state="inactive", rfid="DIR-2")
    make_member(state="noob", rfid="DIR-3")

    body = as_member(caller).get("/api/tools/members/").data

    assert [entry["name"] for entry in body] == ["Test Member1"]
    assert set(body[0]) == {"id", "name", "screenName"}
    assert body[0]["screenName"] == "member1"


def test_the_directory_order_is_deliberately_randomised(
    as_member, make_member, monkeypatch
):
    """``shuffle`` is not incidental -- the frontend shows a random sample.

    Asserting the call rather than the resulting order keeps this from being
    a test that fails once every N runs.
    """
    import api_member_tools.views as views

    calls = []
    monkeypatch.setattr(views, "shuffle", lambda seq: calls.append(list(seq)))
    caller = make_member(state="active", rfid="DIR-4")

    as_member(caller).get("/api/tools/members/")

    assert len(calls) == 1


# --------------------------------------------------------------------------
# /api/tools/issue/
# --------------------------------------------------------------------------


def test_reporting_an_issue_emails_the_admin_and_logs_the_event(
    as_member, make_member, sent_emails, set_config
):
    from profile.models import UserEventLog

    set_config(REPORT_ISSUE_ENABLE_EMAIL=True, EMAIL_ADMIN="admin@example.com")
    member = make_member(state="active", rfid="ISSUE-1")

    response = as_member(member).post(
        "/api/tools/issue/",
        {"title": "Lathe is broken", "description": "It makes a grinding noise"},
        format="json",
    )

    assert response.status_code == 201
    assert response.data == {"success": True}
    (email,) = sent_emails
    assert email["To"] == "admin@example.com"
    assert "Test Member1: Lathe is broken" in email["Subject"]
    assert email["ReplyTo"] == member.user.email
    # send_single_email logs a delivery event of its own against the same user.
    (event,) = UserEventLog.objects.filter(description__startswith="Submitted issue")
    # The reporter's name is prepended to the description they typed.
    assert event.description == (
        "Submitted issue: Lathe is broken "
        "Content: Test Member1: It makes a grinding noise"
    )


def test_an_empty_title_is_rejected(as_member, make_member, integrations_off):
    member = make_member(state="active", rfid="ISSUE-2")

    response = as_member(member).post(
        "/api/tools/issue/", {"title": "", "description": "Something"}, format="json"
    )

    assert response.status_code == 400


def test_an_empty_description_is_accepted(as_member, make_member, integrations_off):
    """DEFECT, pinned: the guard cannot see an empty description.

    ``if not (title and description)`` runs after ``description`` has been
    replaced by ``request.user.profile.get_full_name() + ": " + body[...]``,
    which is non-empty whatever the member typed. Only the title is really
    checked; an issue with no body sails through to every integration.
    """
    member = make_member(state="active", rfid="ISSUE-3")

    response = as_member(member).post(
        "/api/tools/issue/", {"title": "Something", "description": ""}, format="json"
    )

    assert response.status_code == 201


@pytest.mark.parametrize("missing", ["title", "description"])
def test_a_missing_field_raises_instead_of_returning_400(
    as_member, make_member, integrations_off, missing
):
    """DEFECT, pinned: ``body["title"]`` is read before anything is validated.

    The endpoint has a 400 branch, but both fields are indexed out of the
    request body above it, so an absent one is a ``KeyError`` -- a 500 to the
    caller. The same shape as the registration endpoint, pinned in
    ``test_auth_and_session.py``.
    """
    member = make_member(state="active", rfid=f"ISSUE-4-{missing}")
    payload = {"title": "T", "description": "D"}
    del payload[missing]

    with pytest.raises(KeyError, match=missing):
        as_member(member).post("/api/tools/issue/", payload, format="json")


def test_a_failed_delivery_is_still_logged_as_a_submitted_issue(
    as_member, make_member, set_config
):
    """DEFECT, pinned: the audit entry is written before anything is attempted.

    ``request.user.log_event("Submitted issue: ...")`` runs above every
    integration, so the member's log says they reported an issue even when
    all of them failed and the endpoint answered 500. Nothing distinguishes
    a delivered report from a lost one afterwards.

    Trello is switched on here with no network available, which is what the
    autouse ``block_outbound_http`` fixture turns into a failure.
    """
    from profile.models import UserEventLog

    set_config(REPORT_ISSUE_ENABLE_TRELLO=True, REPORT_ISSUE_ENABLE_EMAIL=False)
    member = make_member(state="active", rfid="ISSUE-5")

    response = as_member(member).post(
        "/api/tools/issue/",
        {"title": "Lost report", "description": "Never arrived"},
        format="json",
    )

    assert response.status_code == 500
    (event,) = UserEventLog.objects.filter(description__startswith="Submitted issue")
    assert event.description.startswith("Submitted issue: Lost report")


def test_a_failed_email_answers_500(as_member, make_member, set_config, monkeypatch):
    member = make_member(state="active", rfid="ISSUE-6")
    set_config(REPORT_ISSUE_ENABLE_EMAIL=True)
    monkeypatch.setattr(
        "api_member_tools.views.send_email_to_admin", lambda **kwargs: False
    )

    response = as_member(member).post(
        "/api/tools/issue/",
        {"title": "Bad", "description": "Send fails"},
        format="json",
    )

    assert response.status_code == 500


def test_the_disabled_integrations_are_not_contacted(
    as_member, make_member, integrations_off
):
    """With everything off the endpoint still reports success.

    ``block_outbound_http`` would raise on any real request, so this also
    pins that Vikunja, Trello and Discord stay untouched at their defaults.
    """
    member = make_member(state="active", rfid="ISSUE-7")

    response = as_member(member).post(
        "/api/tools/issue/",
        {"title": "Quiet", "description": "Nothing on"},
        format="json",
    )

    assert response.status_code == 201
