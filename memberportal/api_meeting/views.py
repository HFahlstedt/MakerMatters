from .models import Meeting, ProxyVote
from profile.models import Profile
from django.db import transaction
from django.utils import timezone
from django.utils.timezone import make_aware, localtime
from datetime import datetime

from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from services.emails import send_single_email

PORTAL_DATE_FORMAT = "%Y-%m-%d %H:%M"


def parse_meeting_date(value):
    """
    Read a meeting date as the portal form writes it ("2031-12-01 18:30") or
    as ISO 8601 ("2031-12-01T18:30"). A string with no offset is read in the
    server's TIME_ZONE. Returns None for anything else, so the caller can
    answer 400 instead of raising.
    """
    if not isinstance(value, str):
        return None

    try:
        parsed = datetime.strptime(value, PORTAL_DATE_FORMAT)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None

    return make_aware(parsed) if timezone.is_naive(parsed) else parsed


def email_member(to_user, subject, message, actor):
    send_single_email(
        to_email=to_user.email,
        subject=subject,
        template_vars={"title": subject, "message": message},
        user=actor,
    )


def notify_proxy_removed(proxy, actor, reason):
    """Tell both members that a proxy no longer stands, and why."""
    meeting = proxy.meeting
    when = f"{meeting.get_type()} meeting on {localtime(meeting.date)}"
    holder = proxy.proxy_user.profile.get_full_name()

    subject = f"Your proxy for the {meeting.get_type()} meeting no longer stands"
    email_member(
        proxy.proxy_user,
        subject,
        f"{proxy.user.profile.get_full_name()} is no longer relying on you as a proxy for the {when}: {reason}.",
        actor,
    )
    email_member(
        proxy.user,
        f"{holder} is no longer your proxy for the {meeting.get_type()} meeting",
        f"{holder} is no longer your proxy for the {when}: {reason}. You can manage this proxy from the member portal.",
        actor,
    )


class Meetings(APIView):
    """
    get: retrieves a list of all meetings.
    post: creates a new meeting.
    put: update an existing meeting.
    delete: delete an existing meeting.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        meetings = Meeting.objects.all()

        def get_attendee(attendee):
            return attendee.profile.get_full_name()

        def get_proxy(proxy):
            return {
                "name": proxy.user.profile.get_full_name(),
                "proxyName": proxy.proxy_user.profile.get_full_name(),
                "date": proxy.created_date,
            }

        def get_meeting(meeting):
            return {
                "id": meeting.id,
                "date": meeting.date,
                "chair": meeting.chair,
                "type": meeting.get_type(),
                "typeValue": meeting.type,
                "attendeeCount": meeting.attendees.count(),
                "attendees": list(map(get_attendee, meeting.attendees.all())),
                "proxyList": list(map(get_proxy, meeting.proxyvote_set.all())),
            }

        meetings_object = list(map(get_meeting, meetings))

        return Response(meetings_object)

    def post(self, request):
        body = request.data

        date = parse_meeting_date(body.get("date"))
        if date is None:
            return Response(
                {"error": "date must be YYYY-MM-DD HH:MM or ISO 8601"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meeting_type = body["type"]["value"]
        if meeting_type not in dict(Meeting.MEETING_TYPES):
            return Response(
                {"error": f"unknown meeting type {meeting_type!r}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        Meeting.objects.create(date=date, type=meeting_type, chair=body["chair"])

        return Response({"success": True})

    def put(self, request, meeting_id):
        body = request.data

        date = parse_meeting_date(body.get("date"))
        if date is None:
            return Response(
                {"error": "date must be YYYY-MM-DD HH:MM or ISO 8601"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meeting = Meeting.objects.get(id=meeting_id)
        meeting.date = date
        meeting.chair = body["chair"]
        meeting.save()

        return Response()

    def delete(self, request, meeting_id):
        meeting = Meeting.objects.get(id=meeting_id)
        # The proxies go with the meeting (on_delete=CASCADE); tell the members
        # who relied on them, as revoking one by hand would.
        proxies = list(
            meeting.proxyvote_set.select_related("user", "proxy_user", "meeting")
        )
        meeting.delete()

        for proxy in proxies:
            notify_proxy_removed(proxy, request.user, "the meeting has been deleted")

        return Response()


class Proxies(APIView):
    """
    get: retrieve a list of user's proxy votes.
    post: create a new proxy vote.
    delete: delete an existing proxy vote.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        proxies = ProxyVote.objects.filter(user=request.user)

        def get_proxy_details(proxy):
            return {
                "id": proxy.id,
                "name": proxy.proxy_user.profile.get_full_name(),
                "date": proxy.meeting.date,
                "type": proxy.meeting.get_type(),
            }

        return Response(list(map(get_proxy_details, proxies)))

    def post(self, request):
        body = request.data
        member_city = body.get("memberCity")
        proxy_id = body.get("proxy")
        proxy_city = body.get("proxyCity")
        meeting_id = body.get("meeting")

        # Everything is checked before anything is deleted: a refused request
        # must leave an existing appointment alone.
        if not (member_city and proxy_id and proxy_city and meeting_id):
            return Response(status=status.HTTP_400_BAD_REQUEST)

        meeting = Meeting.objects.filter(pk=meeting_id).first()
        proxy_profile = (
            Profile.objects.filter(id=proxy_id).select_related("user").first()
        )
        if meeting is None or proxy_profile is None:
            return Response(
                {"error": "unknown meeting or proxy"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        proxy_user = proxy_profile.user
        problem = None
        if meeting.date <= timezone.now():
            problem = "that meeting has already taken place"
        elif proxy_user == request.user:
            problem = "you cannot be your own proxy"
        elif proxy_profile.state != "active":
            problem = "a proxy must be an active member"

        if problem:
            return Response({"error": problem}, status=status.HTTP_400_BAD_REQUEST)

        # if the user already has a proxy for this meeting we'll override it
        with transaction.atomic():
            replaced = list(
                ProxyVote.objects.filter(
                    user=request.user, meeting=meeting
                ).select_related("user", "proxy_user", "meeting")
            )
            for proxy in replaced:
                proxy.delete()

            ProxyVote.objects.create(
                user=request.user,
                user_city=member_city,
                proxy_user=proxy_user,
                proxy_city=proxy_city,
                meeting=meeting,
            )

        # The member who was holding the replaced proxy believes they still
        # carry the vote. Skip them if they are the new holder as well.
        for proxy in replaced:
            if proxy.proxy_user != proxy_user:
                email_member(
                    proxy.proxy_user,
                    f"{request.user.profile.get_full_name()} has appointed someone else as their proxy",
                    f"{request.user.profile.get_full_name()} has appointed a different proxy for the {meeting.get_type()} meeting on {localtime(meeting.date)}, so you no longer hold their vote.",
                    request.user,
                )

        subject = f"{request.user.profile.get_full_name()} just assigned you as a proxy"
        message = f"{request.user.profile.get_full_name()} just assigned you as a proxy for the {meeting.get_type()} meeting on {localtime(meeting.date)}."
        email_member(proxy_user, subject, message, request.user)

        subject = f"{proxy_user.profile.get_full_name()} is confirmed as your proxy for the {meeting.get_type()} meeting"
        message = f"{proxy_user.profile.get_full_name()} is confirmed as your proxy for the {meeting.get_type()} meeting on {localtime(meeting.date)}. You can manage this proxy from the member portal."
        email_member(request.user, subject, message, request.user)

        return Response({"success": True})

    def delete(self, request, proxy_id):
        proxy = (
            ProxyVote.objects.filter(id=proxy_id)
            .select_related("user", "proxy_user", "meeting")
            .first()
        )
        if proxy is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if request.user != proxy.user:
            return Response(status=status.HTTP_403_FORBIDDEN)

        proxy.delete()
        subject = f"{request.user.profile.get_full_name()} just removed you as a proxy"
        message = f"{request.user.profile.get_full_name()} just removed you as a proxy for the {proxy.meeting.get_type()} meeting on {localtime(proxy.meeting.date)}."
        email_member(proxy.proxy_user, subject, message, request.user)

        subject = f"{proxy.proxy_user.profile.get_full_name()} is no longer your proxy for the {proxy.meeting.get_type()} meeting"
        message = f"{proxy.proxy_user.profile.get_full_name()} is no longer your proxy for the {proxy.meeting.get_type()} meeting on {localtime(proxy.meeting.date)}. You can manage this proxy from the member portal."
        email_member(request.user, subject, message, request.user)
        return Response()


class MeetingTypes(APIView):
    """
    get: retrieves all of the meeting types.
    """

    def get(self, request):
        def get_type(type):
            return {"label": type[1], "value": type[0]}

        return Response(list(map(get_type, Meeting.MEETING_TYPES)))
