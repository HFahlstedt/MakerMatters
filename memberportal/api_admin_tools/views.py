import json

import stripe
from constance import config
from constance.models import Constance as ConstanceSetting
from constance.codecs import dumps as constance_dumps, loads as constance_loads
from django.conf import settings as django_settings
from django.db.models import F, Sum, Value, CharField, Count, Max
from django.db.models.functions import Concat
from django.db.utils import OperationalError
from rest_framework import permissions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_api_key.permissions import HasAPIKey
from sentry_sdk import capture_exception
from sentry_sdk import capture_message

from access import models
from access.models import DoorLog, InterlockLog
from memberbucks.models import (
    MemberBucks,
    MemberbucksProductPurchaseLog,
)
from profile.models import User, UserEventLog
from services import sms
from services.emails import send_email_to_admin
from .models import MemberTier, PaymentPlan


class StripeAPIView(APIView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not config.ENABLE_STRIPE:
            return

        try:
            stripe.api_key = config.STRIPE_SECRET_KEY
        except OperationalError as error:
            capture_exception(error)


class GetMembers(APIView):
    """
    get: This method returns a list of members.
    """

    permission_classes = (permissions.IsAdminUser | HasAPIKey,)

    def get(self, request):
        filtered = []

        members_queryset = User.objects.select_related("profile")

        screenName = request.GET.get("screenName")
        if screenName is not None:
            members_queryset = members_queryset.filter(profile__screen_name=screenName)

        members = members_queryset.all()

        for member in members:
            filtered.append(member.profile.get_basic_profile())

        return Response(filtered)


class MemberState(APIView):
    """
    get: This method gets a member's state.
    post: This method sets a member's state.
    """

    permission_classes = (permissions.IsAdminUser,)

    def get(self, request, member_id, state=None):
        member = User.objects.get(id=member_id)

        return Response({"state": member.profile.state})

    def post(self, request, member_id, state):
        member = User.objects.get(id=member_id)
        if state == "active":
            member.profile.activate(request)
        elif state == "inactive":
            member.profile.deactivate(request)
        else:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        return Response()


class MakeMember(APIView):
    """
    post: This activates a new member.
    """

    permission_classes = (permissions.IsAdminUser,)

    def post(self, request, member_id):
        user = User.objects.get(id=member_id)

        # if they're a new member or account only
        if user.profile.state == "noob" or user.profile.state == "accountonly":
            # give default door access
            for door in models.Doors.objects.filter(all_members=True):
                user.profile.doors.add(door)

            # give default interlock access
            for interlock in models.Interlock.objects.filter(all_members=True):
                user.profile.interlocks.add(interlock)

            # send the welcome email
            email = user.email_welcome()

            # mark them as "active"
            user.profile.activate()

            subject = f"{user.profile.get_full_name()} just got turned into a member!"
            send_email_to_admin(
                subject=subject,
                template_vars={"title": subject, "message": subject},
                user=request.user,
            )

            if email:
                return Response(
                    {
                        "success": True,
                        "message": "adminTools.makeMemberSuccess",
                    }
                )

            # if there was an error sending the welcome email
            elif email is False:
                return Response(
                    {"success": False, "message": "adminTools.makeMemberErrorEmail"}
                )

            # otherwise some other error happened
            else:
                capture_message("Unknown error occurred when running makemember.")
                return Response(
                    {
                        "success": False,
                        "message": "adminTools.makeMemberError",
                    }
                )
        else:
            return Response(
                {
                    "success": False,
                    "message": "adminTools.makeMemberErrorExists",
                }
            )


class DeviceAdminView(APIView):
    """List, update and delete one device type.

    The three device types share their entire read/update/delete shape and
    differ in only two places: which fields they expose beyond the common set,
    and which usage statistics they aggregate. Subclasses supply those. The
    side effects of an update — granting or revoking default access, pushing a
    maintenance lockout, re-syncing the device — are the same for all of them.
    """

    permission_classes = (permissions.IsAdminUser,)

    #: The concrete device model.
    model = None

    #: Read-only response fields, as {API key: model attribute}.
    readonly_fields = {
        "id": "id",
        "lastSeen": "last_seen",
    }

    #: Fields the admin screen may change, same mapping. The read and write
    #: paths both work from this, so a field cannot become readable but not
    #: writable — which is how the three shapes drifted apart to begin with.
    editable_fields = {
        "name": "name",
        "description": "description",
        "ipAddress": "ip_address",
        "maintenanceLockout": "locked_out",
        "playThemeOnSwipe": "play_theme",
        "exemptFromSignin": "exempt_signin",
        "hiddenToMembers": "hidden",
    }

    def get_statistics(self, device):
        """The usage figures this device type reports. Differs per type."""
        raise NotImplementedError

    def get_device(self, device):
        body = {
            key: getattr(device, attr)
            for mapping in (self.readonly_fields, self.editable_fields)
            for key, attr in mapping.items()
        }
        body["offline"] = device.get_unavailable()
        body.update(self.get_statistics(device))

        return body

    def get(self, request):
        return Response(map(self.get_device, self.model.objects.all()))

    def set_default_access(self, device, granted):
        """Grant or revoke this device for every member, one at a time.

        A single m2m call would do the same thing far more cheaply, but the
        per-member ``profile.save()`` is load-bearing for anything listening
        for profile writes, so the loop stays until that is checked.
        """
        for member in User.objects.all():
            relation = getattr(member.profile, device.profile_relation)

            if granted:
                relation.add(device)
            else:
                relation.remove(device)

            member.profile.save()

    def put(self, request, device_id):
        device = self.model.objects.get(pk=device_id)
        data = request.data

        # All three comparisons must happen before the assignment loop below
        # overwrites the values they are reading.
        default_access_changed = "defaultAccess" in self.editable_fields and (
            device.all_members != data.get("defaultAccess")
        )
        locked_out_changed = device.locked_out != data.get("maintenanceLockout")
        signin_exemption_changed = device.exempt_signin != data.get("exemptFromSignin")

        for key, attr in self.editable_fields.items():
            setattr(device, attr, data.get(key))

        device.save()

        if default_access_changed:
            self.set_default_access(device, granted=data.get("defaultAccess"))

        if locked_out_changed:
            device.send_command("update_device_locked_out")

        if default_access_changed or locked_out_changed or signin_exemption_changed:
            # Push the new tag list, then the new device settings.
            device.sync()
            device.send_command("update_device_object")

        return Response()

    def delete(self, request, device_id):
        self.model.objects.get(pk=device_id).delete()

        return Response()


class Doors(DeviceAdminView):
    """
    get: returns a list of doors.
    put: updates a specific door.
    delete: deletes a specific door.
    """

    model = models.Doors

    # Doors are the only type exposing a serial number and the messaging
    # toggles, and the only one that does not report `authorised`.
    editable_fields = {
        **DeviceAdminView.editable_fields,
        "serialNumber": "serial_number",
        "defaultAccess": "all_members",
        "postDiscordOnSwipe": "post_to_discord",
        "postSlackOnSwipe": "post_to_slack",
    }

    def get_statistics(self, door):
        logs = models.DoorLog.objects.filter(door_id=door.id)

        stats = (
            logs.select_related("user__profile")
            .values("door_id")
            .annotate(
                screen_name=F("user__profile__screen_name"),
                full_name=Concat(
                    F("user__profile__first_name"),
                    Value(" "),
                    F("user__profile__last_name"),
                    output_field=CharField(),
                ),
                total_swipes=Count("door_id"),
                last_swipe=Max("date"),
            )
            .order_by("-total_swipes")
        )

        return {"totalSwipes": logs.count(), "userStats": list(stats)}


class Interlocks(DeviceAdminView):
    """
    get: returns a list of interlocks.
    put: update a specific interlock.
    delete: delete a specific interlock.
    """

    model = models.Interlock

    readonly_fields = {
        **DeviceAdminView.readonly_fields,
        "authorised": "authorised",
    }
    editable_fields = {
        **DeviceAdminView.editable_fields,
        "defaultAccess": "all_members",
    }

    def get_statistics(self, interlock):
        logs = InterlockLog.objects.filter(interlock_id=interlock.id)
        total_time = logs.aggregate(total_time=Sum("total_time")).get("total_time")

        stats = (
            logs.select_related("user_started__profile")
            .values("interlock_id")
            .annotate(
                screen_name=F("user_started__profile__screen_name"),
                full_name=Concat(
                    F("user_started__profile__first_name"),
                    Value(" "),
                    F("user_started__profile__last_name"),
                    output_field=CharField(),
                ),
                total_swipes=Count("total_time"),
                total_seconds=Sum("total_time"),
            )
            .order_by("-total_seconds", "-total_swipes")
        )

        return {
            "totalTimeSeconds": total_time.total_seconds() if total_time else 0,
            "userStats": list(stats),
        }


class MemberbucksDevices(DeviceAdminView):
    """
    get: returns a list of memberbucks devices.
    put: update a specific memberbucks device.
    delete: delete a specific memberbucks device.
    """

    model = models.MemberbucksDevice

    readonly_fields = {
        **DeviceAdminView.readonly_fields,
        "authorised": "authorised",
    }
    # No `defaultAccess`: a vending machine has no per-member access list, so
    # the flag would have nothing to gate in either position.

    def get_statistics(self, device):
        purchases = MemberbucksProductPurchaseLog.objects.filter(
            memberbucks_device_id=device.id, success=True
        )
        total_volume = (
            purchases.aggregate(total_volume=Sum("price")).get("total_volume") or 0
        ) / 100

        stats = (
            purchases.select_related("user__profile")
            .values("memberbucks_device_id")
            .annotate(
                screen_name=F("user__profile__screen_name"),
                full_name=Concat(
                    F("user__profile__first_name"),
                    Value(" "),
                    F("user__profile__last_name"),
                    output_field=CharField(),
                ),
                total_purchases=Count("price"),
                total_volume=(Sum("price") or 0) / 100,
            )
            .order_by("-total_purchases", "-total_volume")
        )

        return {
            "totalPurchases": purchases.count(),
            "totalVolume": total_volume,
            "userStats": list(stats),
        }


class MemberAccess(APIView):
    """
    get: This method gets a member's access permissions.
    """

    permission_classes = (permissions.IsAdminUser | HasAPIKey,)

    def get(self, request, member_id):
        member = User.objects.get(id=member_id)

        return Response(member.profile.get_access_permissions(ignore_user_state=True))


class MemberWelcomeEmail(APIView):
    """
    post: This method sends a welcome email to the specified member.
    """

    permission_classes = (permissions.IsAdminUser,)

    def post(self, request, member_id):
        member = User.objects.get(id=member_id)
        member.email_welcome()

        return Response()


class MemberSendSms(APIView):
    """
    post: This method sends a custom sms alert to the specified member.
    """

    permission_classes = (permissions.IsAdminUser,)

    def post(self, request, member_id):
        member = User.objects.get(id=member_id)
        sms_body = request.data["smsBody"]

        if not config.SMS_ENABLE:
            return Response(
                {"success": False, "message": "SMS functionality not enabled."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if not member.profile.phone:
            return Response(
                {"success": False, "message": "Member does not have a phone number."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # check if the sms body exists, is at least 1 character, and isn't more than 320 characters
        if not sms_body or len(sms_body) < 1 or len(sms_body) > 320:
            return Response(
                {"success": False, "message": "SMS body is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sms_message = sms.SMS()
        sms_message.send_custom_notification(
            to_number=member.profile.phone,
            message=sms_body,
            portal_user_sender=request.user,
            portal_user_recipient=member,
        )

        return Response()


class MemberProfile(APIView):
    """
    put: This method updates a member's profile.
    """

    permission_classes = (permissions.IsAdminUser,)

    def put(self, request, member_id):
        if not member_id:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        body = json.loads(request.body)
        member = User.objects.get(id=member_id)
        rfid_changed = False

        if member.profile.rfid != body.get("rfidCard"):
            rfid_changed = True

        member.email = body.get("email")
        member.profile.first_name = body.get("firstName")
        member.profile.last_name = body.get("lastName")
        member.profile.rfid = body.get("rfidCard")
        member.profile.phone = body.get("phone")
        member.profile.screen_name = body.get("screenName")
        member.profile.vehicle_registration_plate = body.get("vehicleRegistrationPlate")
        member.profile.exclude_from_email_export = body.get("excludeFromEmailExport")

        member.save()
        member.profile.save()

        if rfid_changed:
            for door in member.profile.doors.all():
                door.sync()

        return Response()


class ManageMembershipTier(StripeAPIView):
    """
    get: gets a membership tier.
    post: creates a new membership tier.
    put: updates a membership tier.
    delete: deletes a membership tier.
    """

    permission_classes = (permissions.IsAdminUser,)

    def get_tier(self, tier: MemberTier):
        return {
            "id": tier.id,
            "name": tier.name,
            "description": tier.description,
            "visible": tier.visible,
            "featured": tier.featured,
            "stripeId": tier.stripe_id,
        }

    def get(self, request, tier_id=None):
        if tier_id:
            try:
                tier = MemberTier.objects.get(pk=tier_id)
                return Response(self.get_tier(tier))

            except MemberTier.DoesNotExist as e:
                return Response(status=status.HTTP_404_NOT_FOUND)

        else:
            formatted_tiers = []

            for tier in MemberTier.objects.all():
                formatted_tiers.append(self.get_tier(tier))

            return Response(formatted_tiers)

    def post(self, request):
        body = request.data

        try:
            product = stripe.Product.create(
                name=body["name"], description=body["description"]
            )
            tier = MemberTier.objects.create(
                name=body["name"],
                description=body["description"],
                visible=body["visible"],
                featured=body["featured"],
                stripe_id=product.id,
            )

            return Response(self.get_tier(tier))

        except stripe.error.AuthenticationError:
            return Response(
                {"success": False, "message": "error.stripeNotConfigured"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def put(self, request, tier_id):
        body = request.data

        tier = MemberTier.objects.get(pk=tier_id)

        tier.name = body["name"]
        tier.description = body["description"]
        tier.visible = body["visible"]
        tier.featured = body["featured"]
        tier.save()

        return Response(self.get_tier(tier))

    def delete(self, request, tier_id):
        tier = MemberTier.objects.get(pk=tier_id)
        tier.delete()

        return Response()


class ManageMembershipTierPlan(StripeAPIView):
    """
    get: gets an individual or a list of payment plans.
    post: creates a new payment plan.

    """

    permission_classes = (permissions.IsAdminUser,)

    def get_plan(self, plan: PaymentPlan):
        return {
            "id": plan.id,
            "name": plan.name,
            "stripeId": plan.stripe_id,
            "memberTier": plan.member_tier.id,
            "visible": plan.visible,
            "currency": plan.currency,
            "cost": plan.cost,  # cents, matching the model and the create path
            "intervalCount": plan.interval_count,
            "interval": plan.interval,
        }

    def get(self, request, plan_id=None, tier_id=None):
        if plan_id:
            try:
                plan = PaymentPlan.objects.get(pk=plan_id)
                return Response(self.get_plan(plan))

            except PaymentPlan.DoesNotExist as e:
                return Response(status=status.HTTP_404_NOT_FOUND)

        if tier_id:
            try:
                formatted_plans = []

                for plan in PaymentPlan.objects.filter(member_tier=tier_id):
                    formatted_plans.append(self.get_plan(plan))

                return Response(formatted_plans)

            except PaymentPlan.DoesNotExist as e:
                return Response(status=status.HTTP_404_NOT_FOUND)

        else:
            formatted_plans = []

            for plan in PaymentPlan.objects.all():
                formatted_plans.append(self.get_plan(plan))

            return Response(formatted_plans)

    def post(self, request, tier_id=None):
        if tier_id is not None:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        body = request.data

        member_tier = MemberTier.objects.get(pk=body["memberTier"])

        stripe_plan = stripe.Price.create(
            unit_amount=round(body["cost"]),
            currency=str(body["currency"]).lower(),
            recurring={
                "interval": body["interval"],
                "interval_count": body["intervalCount"],
            },
            product=member_tier.stripe_id,
        )

        plan = PaymentPlan.objects.create(
            name=body["name"],
            stripe_id=stripe_plan.id,
            member_tier_id=body["memberTier"],
            visible=body["visible"],
            currency=str(body["currency"]).lower(),
            cost=round(body["cost"]),
            interval_count=body["intervalCount"],
            interval=body["interval"],
        )

        return Response(self.get_plan(plan))

    def put(self, request, plan_id):
        body = request.data

        plan = PaymentPlan.objects.get(pk=plan_id)

        plan.name = body["name"]
        plan.visible = body["visible"]
        plan.cost = round(body["cost"])  # cents, as in post()
        plan.save()

        return Response(self.get_plan(plan))

    def delete(self, request, plan_id):
        plan = PaymentPlan.objects.get(pk=plan_id)
        plan.delete()

        return Response()


class MemberBillingInfo(StripeAPIView):
    """
    get: This method gets a member's billing info.
    """

    permission_classes = (permissions.IsAdminUser | HasAPIKey,)

    def get(self, request, member_id):
        member = User.objects.get(id=member_id)
        current_plan = member.profile.membership_plan

        billing_info = {}

        if current_plan:
            s = None

            # if we have a subscription id, fetch the details
            if member.profile.stripe_subscription_id:
                s = stripe.Subscription.retrieve(
                    member.profile.stripe_subscription_id,
                )

            # if we got subscription details
            if s:
                billing_info["subscription"] = {
                    "status": member.profile.subscription_status,
                    "billingCycleAnchor": s.billing_cycle_anchor,
                    "currentPeriodEnd": s.current_period_end,
                    "cancelAt": s.cancel_at,
                    "cancelAtPeriodEnd": s.cancel_at_period_end,
                    "startDate": s.start_date,
                    "membershipTier": member.profile.membership_plan.member_tier.get_object(),
                    "membershipPlan": member.profile.membership_plan.get_object(),
                }
            else:
                billing_info["subscription"] = None

        # get the most recent memberbucks transactions and order them by date
        recent_transactions = MemberBucks.objects.filter(user=member).order_by("date")[
            ::-1
        ][:100]

        def get_transaction(transaction):
            return transaction.get_transaction_display()

        billing_info["memberbucks"] = {
            "balance": member.profile.memberbucks_balance,
            "stripe_card_last_digits": member.profile.stripe_card_last_digits,
            "stripe_card_expiry": member.profile.stripe_card_expiry,
            "transactions": map(get_transaction, recent_transactions),
            "lastPurchase": member.profile.last_memberbucks_purchase,
        }

        return Response(billing_info)


class MemberLogs(APIView):
    """
    get: This method gets a member's logs.
    """

    permission_classes = (permissions.IsAdminUser | HasAPIKey,)

    def get(self, request, member_id):
        user = User.objects.get(id=member_id)

        user_event_logs = []
        door_logs = []
        interlock_logs = []

        for user_event_log in UserEventLog.objects.order_by("-date").filter(user=user)[
            :1000
        ]:
            user_event_logs.append(
                {
                    "date": user_event_log.date,
                    "description": user_event_log.description,
                    "logtype": user_event_log.get_logtype_display(),
                }
            )

        for door_log in DoorLog.objects.order_by("-date").filter(user=user)[:500]:
            door_logs.append(
                {
                    "date": door_log.date,
                    "door": door_log.door.name,
                    "success": door_log.success,
                }
            )

        for interlock_log in InterlockLog.objects.filter(user_started=user)[:1000]:
            status = None

            if not interlock_log.success:
                status = -1
            else:
                status = 1 if interlock_log.date_ended else 0

            interlock_logs.append(
                {
                    "interlockName": interlock_log.interlock.name,
                    "dateStarted": interlock_log.date_started,
                    "totalTime": interlock_log.total_time,
                    "totalCost": (interlock_log.total_cost or 0) / 100,
                    "status": status,
                    "userEnded": (
                        interlock_log.user_ended.get_full_name()
                        if interlock_log.user_ended
                        else None
                    ),
                }
            )

        logs = {
            "userEventLogs": user_event_logs,
            "doorLogs": door_logs,
            "interlockLogs": interlock_logs,
        }

        return Response(logs)


class ManageSettings(APIView):
    """
    get: This method gets a constance setting value or values.
    put: This method updates a constance setting value.
    """

    permission_classes = (permissions.IsAdminUser,)

    def get_setting(self, setting):
        # django-constance 4 stores values as a JSON envelope
        # ({"__type__": ..., "__value__": ...}) in a plain TextField. Up to
        # constance 2.x the column was a PickledObjectField, which decoded on
        # attribute access, so `setting.value` was the Python value. Decode
        # explicitly to keep this endpoint returning what it always returned.
        try:
            value = constance_loads(setting.value)
        except (ValueError, TypeError):
            # A row written before the codec existed, or hand-edited.
            value = setting.value

        return {
            "key": setting.key,
            "value": value,
        }

    def get(self, request, setting_key=None):
        if setting_key:
            try:
                setting = ConstanceSetting.objects.get(key=setting_key)
                return Response(self.get_setting(setting))

            except ConstanceSetting.DoesNotExist as e:
                return Response(status=status.HTTP_404_NOT_FOUND)

        else:
            settings = []

            for setting in ConstanceSetting.objects.all():
                settings.append(self.get_setting(setting))

            return Response(settings)

    @staticmethod
    def matches_declared_type(value, default):
        """Whether ``value`` may be stored for a setting declared with ``default``.

        Constance takes the type of the declared default as the setting's type,
        but stores whatever it is given. Without this check the string "false"
        can be written to a boolean, and every ``config.X`` test in the codebase
        then reads a non-empty string as True.

        ``bool`` is checked before ``int`` because it is a subclass of it, and
        an int is accepted for a float because JSON does not distinguish 2
        from 2.0.
        """
        if isinstance(default, bool):
            return isinstance(value, bool)

        if isinstance(default, float):
            return isinstance(value, (int, float)) and not isinstance(value, bool)

        if isinstance(default, int):
            return isinstance(value, int) and not isinstance(value, bool)

        return isinstance(value, type(default))

    def put(self, request, setting_key=None):
        if not setting_key:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        body = request.data

        declared = django_settings.CONSTANCE_CONFIG.get(setting_key)
        if declared and not self.matches_declared_type(body["value"], declared[0]):
            return Response(
                {
                    "error": f"{setting_key} is declared as "
                    f"{type(declared[0]).__name__}."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            setting = ConstanceSetting.objects.get(key=setting_key)
            setting.value = constance_dumps(body["value"])
            setting.save()

            return Response(self.get_setting(setting))

        except ConstanceSetting.DoesNotExist as e:
            return Response(status=status.HTTP_404_NOT_FOUND)
