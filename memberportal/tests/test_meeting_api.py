"""Characterisation: general meetings and proxy votes.

``api_meeting`` has two halves. Admins schedule meetings under
``/api/meetings/``; members appoint a proxy -- another member to vote on
their behalf -- under ``/api/proxies/``. Proxy records are governance data:
at an AGM they decide whose votes a member carries, which is why so many of
the tests below are about records disappearing or being accepted when they
should not be.

The member-facing proxy form offers only upcoming meetings (from
``/api/tools/meetings/``) and only active members (from
``/api/tools/members/``), and it sends that directory's id -- a **Profile**
id, not a User id. None of those limits are enforced here: every one of them
lives in the frontend alone, so any client that is not the portal's own form
can appoint a proxy for a past meeting, for themselves, or held by someone
who has left.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from django.utils.timezone import localtime

pytestmark = pytest.mark.django_db


MEETING_FIELDS = {
    "id",
    "date",
    "chair",
    "type",
    "typeValue",
    "attendeeCount",
    "attendees",
    "proxyList",
}


@pytest.fixture(autouse=True)
def capture_email(sent_emails):
    """Appointing or revoking a proxy sends two emails; none may leave the test.

    Tests that assert on those emails still ask for ``sent_emails`` by name,
    and receive this same list.
    """
    return sent_emails


def proxy_body(meeting, holder, **overrides):
    """What ``ProxyForm.vue`` posts."""
    body = {
        "meeting": meeting.id,
        "memberCity": "Brisbane",
        "proxy": holder.id,
        "proxyCity": "Gold Coast",
    }
    body.update(overrides)
    return body


def client_for(profile):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=profile.user)
    return client


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/api/meetings/", "/api/meetings/types/", "/api/proxies/"]
)
def test_an_anonymous_caller_is_refused(api_client, path):
    assert api_client.get(path).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/meetings/"),
        ("post", "/api/meetings/"),
        ("put", "/api/meetings/{id}/"),
        ("delete", "/api/meetings/{id}/"),
    ],
)
def test_a_plain_member_cannot_manage_meetings(make_member, make_meeting, method, path):
    member = make_member(state="active", rfid="MEET-AUTH")
    meeting = make_meeting()

    response = getattr(client_for(member), method)(
        path.format(id=meeting.id), {}, format="json"
    )

    assert response.status_code == 403


def test_meeting_types_are_open_to_any_member(make_member):
    """``MeetingTypes`` declares no permission_classes of its own.

    Like ``Members`` in ``api_member_tools``, it is closed to anonymous callers
    only by ``DEFAULT_PERMISSION_CLASSES``.
    """
    from api_meeting.views import MeetingTypes

    member = make_member(state="active", rfid="MEET-TYPES")

    assert "permission_classes" not in MeetingTypes.__dict__
    assert client_for(member).get("/api/meetings/types/").data == [
        {"label": "General", "value": "general"},
        {"label": "Annual General", "value": "agm"},
        {"label": "Other", "value": "other"},
    ]


# --------------------------------------------------------------------------
# /api/meetings/ (admin)
# --------------------------------------------------------------------------


def test_the_admin_meeting_list_includes_each_meetings_proxies(
    as_admin, make_member, make_meeting
):
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="MEET-LIST-1")
    holder = make_member(state="active", rfid="MEET-LIST-2")
    meeting = make_meeting(type="agm", chair="Sam")
    ProxyVote.objects.create(user=giver.user, proxy_user=holder.user, meeting=meeting)

    (entry,) = as_admin().get("/api/meetings/").data

    assert set(entry) == MEETING_FIELDS
    assert (entry["type"], entry["typeValue"], entry["chair"]) == (
        "Annual General",
        "agm",
        "Sam",
    )
    (proxy,) = entry["proxyList"]
    assert set(proxy) == {"name", "proxyName", "date"}
    assert (proxy["name"], proxy["proxyName"]) == ("Test Member1", "Test Member2")


def test_attendance_is_reported_but_nothing_in_the_api_records_it(
    as_admin, make_member, make_meeting
):
    """``attendees`` is read here and written nowhere in the codebase.

    Only the Django admin can populate the many-to-many, so through the portal
    ``attendeeCount`` is always 0. What is pinned is that the endpoint does
    report whatever the relation holds.
    """
    member = make_member(state="active", rfid="MEET-ATTEND")
    meeting = make_meeting()

    assert as_admin().get("/api/meetings/").data[0]["attendeeCount"] == 0

    meeting.attendees.add(member.user)
    (entry,) = as_admin().get("/api/meetings/").data

    assert entry["attendeeCount"] == 1
    assert entry["attendees"] == ["Test Member1"]


def test_an_admin_creates_a_meeting_in_the_servers_timezone(as_admin, settings):
    from api_meeting.models import Meeting

    response = as_admin().post(
        "/api/meetings/",
        {"date": "2031-12-01 18:30", "type": {"value": "general"}, "chair": "Sam"},
        format="json",
    )

    assert response.data == {"success": True}
    meeting = Meeting.objects.get()
    # The string carries no offset; make_aware reads it as TIME_ZONE.
    assert localtime(meeting.date).strftime("%Y-%m-%d %H:%M") == "2031-12-01 18:30"
    assert (meeting.type, meeting.chair) == ("general", "Sam")


def test_a_meeting_date_in_iso_format_is_accepted(as_admin):
    """Used to raise ``ValueError`` (a 500): one exact format, no fallback.

    ``strptime(body["date"], "%Y-%m-%d %H:%M")`` accepted the portal form's
    format and nothing else, so the ISO ``2031-12-01T18:30`` any other client
    would send blew up. ISO is read too now; an offset is honoured, and a
    string without one is read in TIME_ZONE as before.
    """
    from api_meeting.models import Meeting

    response = as_admin().post(
        "/api/meetings/",
        {"date": "2031-12-01T18:30", "type": {"value": "general"}, "chair": "Sam"},
        format="json",
    )

    assert response.status_code == 200
    assert localtime(Meeting.objects.get().date).strftime("%Y-%m-%d %H:%M") == (
        "2031-12-01 18:30"
    )


@pytest.mark.parametrize("date", ["next tuesday", "", None, 20311201])
def test_a_meeting_date_that_cannot_be_read_is_a_400(as_admin, date):
    from api_meeting.models import Meeting

    response = as_admin().post(
        "/api/meetings/",
        {"date": date, "type": {"value": "general"}, "chair": "Sam"},
        format="json",
    )

    assert response.status_code == 400
    assert not Meeting.objects.exists()


def test_a_meeting_cannot_be_created_with_an_undeclared_type(as_admin):
    """Used to store the type without checking ``MEETING_TYPES``.

    Choices are not enforced by the database, so "picnic" saved. The admin list
    then showed the raw value as its display name, and a proxy email called it
    "the picnic meeting". It is a 400 now and nothing is written.
    """
    from api_meeting.models import Meeting

    response = as_admin().post(
        "/api/meetings/",
        {"date": "2031-12-01 18:30", "type": {"value": "picnic"}, "chair": "Sam"},
        format="json",
    )

    assert response.status_code == 400
    assert not Meeting.objects.exists()


def test_an_update_changes_date_and_chair_but_never_the_type(as_admin, make_meeting):
    """By design: ``MeetingForm.vue`` disables the type field when editing.

    The body's ``type`` is ignored rather than rejected.
    """
    meeting = make_meeting(type="agm", chair="Sam")

    as_admin().put(
        f"/api/meetings/{meeting.id}/",
        {"date": "2031-06-01 19:00", "type": {"value": "other"}, "chair": "Alex"},
        format="json",
    )

    meeting.refresh_from_db()
    assert localtime(meeting.date).strftime("%Y-%m-%d %H:%M") == "2031-06-01 19:00"
    assert (meeting.type, meeting.chair) == ("agm", "Alex")


def test_a_past_meeting_can_still_be_moved(as_admin, make_meeting):
    """The form refuses to edit a past meeting's date; the endpoint does not.

    Another rule that lives only in the frontend. For an admin-only endpoint
    that is a smaller gap than on the proxy side, but it means a meeting whose
    proxies were collected can be moved after the fact.
    """
    meeting = make_meeting(date=timezone.now() - timedelta(days=10))

    response = as_admin().put(
        f"/api/meetings/{meeting.id}/",
        {"date": "2031-06-01 19:00", "chair": "Sam"},
        format="json",
    )

    assert response.status_code == 200
    meeting.refresh_from_db()
    assert meeting.date > timezone.now()


def test_deleting_a_meeting_tells_the_members_its_proxies_go_with_it(
    as_admin, make_member, make_meeting, sent_emails
):
    """Used to be a cascade nobody was told about.

    ``ProxyVote.meeting`` is ``on_delete=CASCADE``, so every proxy attached to
    a deleted meeting goes with it. Revoking a single proxy emails both
    members, but deleting the meeting that held fifty emailed nobody. Both
    members of each proxy are emailed now, as a revocation would.
    """
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="MEET-DEL-1")
    holder = make_member(state="active", rfid="MEET-DEL-2")
    meeting = make_meeting()
    ProxyVote.objects.create(user=giver.user, proxy_user=holder.user, meeting=meeting)

    response = as_admin().delete(f"/api/meetings/{meeting.id}/")

    assert response.status_code == 200
    assert not ProxyVote.objects.exists()
    assert [(e["To"], e["Subject"]) for e in sent_emails] == [
        (holder.user.email, "Your proxy for the General meeting no longer stands"),
        (
            giver.user.email,
            "Test Member2 is no longer your proxy for the General meeting",
        ),
    ]


# --------------------------------------------------------------------------
# /api/proxies/: appointing
# --------------------------------------------------------------------------


def test_appointing_a_proxy_records_it_and_emails_both_members(
    make_member, make_meeting, sent_emails
):
    """The ``proxy`` field is a Profile id, as ``/api/tools/members/`` returns.

    The two id sequences coincide in a fresh database, so a profile-less user
    is created first to force them apart.
    """
    from api_meeting.models import ProxyVote
    from profile.models import User

    giver = make_member(state="active", rfid="PROXY-1")
    User.objects.create(email="orphan@example.com")
    holder = make_member(state="active", rfid="PROXY-2")
    assert holder.id != holder.user.id
    meeting = make_meeting(type="agm")

    response = client_for(giver).post(
        "/api/proxies/", proxy_body(meeting, holder), format="json"
    )

    assert response.data == {"success": True}
    vote = ProxyVote.objects.get()
    assert (vote.user, vote.proxy_user, vote.meeting) == (
        giver.user,
        holder.user,
        meeting,
    )
    assert (vote.user_city, vote.proxy_city) == ("Brisbane", "Gold Coast")
    assert [(e["To"], e["Subject"]) for e in sent_emails] == [
        (holder.user.email, "Test Member1 just assigned you as a proxy"),
        (
            giver.user.email,
            "Test Member2 is confirmed as your proxy for the Annual General meeting",
        ),
    ]


def test_a_member_lists_the_proxies_they_have_appointed(make_member, make_meeting):
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="PROXY-3")
    holder = make_member(state="active", rfid="PROXY-4")
    vote = ProxyVote.objects.create(
        user=giver.user, proxy_user=holder.user, meeting=make_meeting(type="agm")
    )

    (entry,) = client_for(giver).get("/api/proxies/").data

    assert set(entry) == {"id", "name", "date", "type"}
    assert (entry["id"], entry["name"], entry["type"]) == (
        vote.id,
        "Test Member2",
        "Annual General",
    )


def test_a_member_cannot_see_the_proxies_they_hold(make_member, make_meeting):
    """``GET /api/proxies/`` filters on who appointed, never on who holds.

    The only record a holder receives is the email. Nothing in the portal
    tells a member how many votes they will be carrying into a meeting.
    """
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="PROXY-5")
    holder = make_member(state="active", rfid="PROXY-6")
    ProxyVote.objects.create(
        user=giver.user, proxy_user=holder.user, meeting=make_meeting()
    )

    assert client_for(holder).get("/api/proxies/").data == []


def test_the_cities_are_recorded_but_never_returned(
    as_admin, make_member, make_meeting
):
    """Both cities are required to appoint a proxy and appear in no response.

    Neither the member's list nor the admin's meeting list includes them, so
    they are reachable only through the Django admin.
    """
    giver = make_member(state="active", rfid="PROXY-CITY-1")
    holder = make_member(state="active", rfid="PROXY-CITY-2")
    meeting = make_meeting()
    client_for(giver).post("/api/proxies/", proxy_body(meeting, holder), format="json")

    member_view = client_for(giver).get("/api/proxies/").data
    admin_view = as_admin().get("/api/meetings/").data

    for payload in (member_view, admin_view):
        assert "Brisbane" not in str(payload)
        assert "Gold Coast" not in str(payload)


def test_one_member_can_hold_any_number_of_proxies(make_member, make_meeting):
    """Pinned as a decision, not a defect: there is no cap.

    Many constitutions limit how many proxies one member may carry. Whether
    this space has such a rule is not something the code can know.
    """
    from api_meeting.models import ProxyVote

    holder = make_member(state="active", rfid="PROXY-HOLDER")
    meeting = make_meeting()
    for i in range(5):
        giver = make_member(state="active", rfid=f"PROXY-GIVER-{i}")
        client_for(giver).post(
            "/api/proxies/", proxy_body(meeting, holder), format="json"
        )

    assert ProxyVote.objects.filter(proxy_user=holder.user).count() == 5


# --------------------------------------------------------------------------
# /api/proxies/: what is accepted that should not be
# --------------------------------------------------------------------------


def test_a_proxy_cannot_be_appointed_for_a_meeting_that_has_passed(
    make_member, make_meeting
):
    """Used to accept any meeting id: the meeting's date was never checked.

    The form offers only upcoming meetings. (Until the frozen cutoff in
    ``MeetingList`` was fixed, it offered past meetings too, so this was
    reachable from the portal itself.) The endpoint now refuses with a 400.
    """
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="PROXY-PAST-1")
    holder = make_member(state="active", rfid="PROXY-PAST-2")
    past = make_meeting(date=timezone.now() - timedelta(days=30))

    response = client_for(giver).post(
        "/api/proxies/", proxy_body(past, holder), format="json"
    )

    assert response.status_code == 400
    assert not ProxyVote.objects.exists()


def test_a_member_cannot_appoint_themselves_as_their_own_proxy(
    make_member, make_meeting
):
    """Used to be accepted: nothing compared the two members."""
    from api_meeting.models import ProxyVote

    member = make_member(state="active", rfid="PROXY-SELF")

    response = client_for(member).post(
        "/api/proxies/", proxy_body(make_meeting(), member), format="json"
    )

    assert response.status_code == 400
    assert not ProxyVote.objects.exists()


@pytest.mark.parametrize("state", ["inactive", "noob", "accountonly"])
def test_a_member_who_is_not_active_cannot_be_appointed(
    make_member, make_meeting, state
):
    """Used to accept any profile id: the holder's state was never checked.

    The directory the form draws from lists active members only; the endpoint
    took anyone, including someone whose membership had lapsed.
    """
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid=f"PROXY-STATE-A-{state}")
    holder = make_member(state=state, rfid=f"PROXY-STATE-B-{state}")

    response = client_for(giver).post(
        "/api/proxies/", proxy_body(make_meeting(), holder), format="json"
    )

    assert response.status_code == 400
    assert not ProxyVote.objects.exists()


# --------------------------------------------------------------------------
# /api/proxies/: replacing, and what a bad request destroys
# --------------------------------------------------------------------------


def test_replacing_a_proxy_tells_the_member_who_was_replaced(
    make_member, make_meeting, sent_emails
):
    """Used to delete the old appointment without notice.

    Appointing a second proxy for the same meeting deletes the first and
    creates the new one. The new holder and the member were both emailed; the
    member who had been holding the proxy was not, and would still believe they
    were carrying that vote. They are emailed first now, as a revocation would.
    """
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="PROXY-SWAP-1")
    first = make_member(state="active", rfid="PROXY-SWAP-2")
    second = make_member(state="active", rfid="PROXY-SWAP-3")
    meeting = make_meeting()
    client = client_for(giver)
    client.post("/api/proxies/", proxy_body(meeting, first), format="json")
    sent_emails.clear()

    client.post("/api/proxies/", proxy_body(meeting, second), format="json")

    assert ProxyVote.objects.get().proxy_user == second.user
    recipients = [email["To"] for email in sent_emails]
    assert recipients == [first.user.email, second.user.email, giver.user.email]


def test_resubmitting_the_same_holder_does_not_email_them_twice(
    make_member, make_meeting, sent_emails
):
    """Changing only the cities replaces the row but not the holder."""
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="PROXY-SAME-1")
    holder = make_member(state="active", rfid="PROXY-SAME-2")
    meeting = make_meeting()
    client = client_for(giver)
    client.post("/api/proxies/", proxy_body(meeting, holder), format="json")
    sent_emails.clear()

    client.post(
        "/api/proxies/", proxy_body(meeting, holder, proxyCity="Malmo"), format="json"
    )

    assert ProxyVote.objects.get().proxy_city == "Malmo"
    assert [email["To"] for email in sent_emails] == [
        holder.user.email,
        giver.user.email,
    ]


@pytest.mark.parametrize("missing", ["memberCity", "proxy", "proxyCity"])
def test_an_incomplete_resubmission_leaves_the_existing_proxy_alone(
    make_member, make_meeting, sent_emails, missing
):
    """Used to delete the old proxy before the request was checked.

    ``post`` deleted any proxy the member already held for that meeting, and
    only then tested whether the new request was complete. An incomplete one
    was refused with a 400 -- after destroying a valid appointment, and without
    an email to either member. Nothing is touched now.
    """
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid=f"PROXY-BAD-1-{missing}")
    holder = make_member(state="active", rfid=f"PROXY-BAD-2-{missing}")
    meeting = make_meeting()
    client = client_for(giver)
    client.post("/api/proxies/", proxy_body(meeting, holder), format="json")
    sent_emails.clear()

    response = client.post(
        "/api/proxies/", proxy_body(meeting, holder, **{missing: ""}), format="json"
    )

    assert response.status_code == 400
    assert ProxyVote.objects.get().proxy_user == holder.user
    assert sent_emails == []


def test_an_unknown_holder_is_a_400_and_the_existing_proxy_survives(
    make_member, make_meeting
):
    """Used to raise after the existing proxy was already gone.

    With a complete request naming a profile that does not exist,
    ``Profile.objects.get`` raised -- a 500 -- once the deletion had run.
    ``ATOMIC_REQUESTS`` is not set in settings.py, so in production that
    deletion was committed on its own and the member was left with no proxy.
    """
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="PROXY-GHOST-1")
    holder = make_member(state="active", rfid="PROXY-GHOST-2")
    meeting = make_meeting()
    client = client_for(giver)
    client.post("/api/proxies/", proxy_body(meeting, holder), format="json")

    response = client.post(
        "/api/proxies/", proxy_body(meeting, holder, proxy=99999), format="json"
    )

    assert response.status_code == 400
    assert ProxyVote.objects.get().proxy_user == holder.user


def test_an_unknown_meeting_is_a_400(make_member, make_meeting):
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="PROXY-NOMEET-1")
    holder = make_member(state="active", rfid="PROXY-NOMEET-2")

    response = client_for(giver).post(
        "/api/proxies/",
        proxy_body(make_meeting(), holder) | {"meeting": 99999},
        format="json",
    )

    assert response.status_code == 400
    assert not ProxyVote.objects.exists()


# --------------------------------------------------------------------------
# /api/proxies/<id>/: revoking
# --------------------------------------------------------------------------


def test_revoking_a_proxy_deletes_it_and_emails_both_members(
    make_member, make_meeting, sent_emails
):
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid="PROXY-REVOKE-1")
    holder = make_member(state="active", rfid="PROXY-REVOKE-2")
    vote = ProxyVote.objects.create(
        user=giver.user, proxy_user=holder.user, meeting=make_meeting(type="agm")
    )

    response = client_for(giver).delete(f"/api/proxies/{vote.id}/")

    assert response.status_code == 200
    assert not ProxyVote.objects.exists()
    assert [(e["To"], e["Subject"]) for e in sent_emails] == [
        (holder.user.email, "Test Member1 just removed you as a proxy"),
        (
            giver.user.email,
            "Test Member2 is no longer your proxy for the Annual General meeting",
        ),
    ]


@pytest.mark.parametrize("who", ["holder", "bystander"])
def test_only_the_appointing_member_can_revoke(
    make_member, make_meeting, sent_emails, who
):
    """Not even the holder can give a proxy back."""
    from api_meeting.models import ProxyVote

    giver = make_member(state="active", rfid=f"PROXY-OWN-1-{who}")
    holder = make_member(state="active", rfid=f"PROXY-OWN-2-{who}")
    bystander = make_member(state="active", rfid=f"PROXY-OWN-3-{who}")
    vote = ProxyVote.objects.create(
        user=giver.user, proxy_user=holder.user, meeting=make_meeting()
    )
    caller = holder if who == "holder" else bystander

    response = client_for(caller).delete(f"/api/proxies/{vote.id}/")

    assert response.status_code == 403
    assert ProxyVote.objects.filter(pk=vote.pk).exists()
    assert sent_emails == []


def test_revoking_an_unknown_proxy_is_a_404(make_member):
    """Used to be an unchecked ``ProxyVote.objects.get(id=...)`` -- a 500."""
    member = make_member(state="active", rfid="PROXY-UNKNOWN")

    response = client_for(member).delete("/api/proxies/99999/")

    assert response.status_code == 404
