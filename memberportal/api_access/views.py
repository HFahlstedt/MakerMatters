from access.models import (
    Doors,
    Interlock,
    MemberbucksDevice,
    HasExternalAccessControlAPIKey,
)
from profile.models import User
import api_access.metrics as metrics

from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from constance import config


def get_device(door_id=None, interlock_id=None, memberbucks_device_id=None):
    """Resolve the device a URL refers to.

    Several of these routes are registered for more than one device type, so
    which keyword argument arrives depends on which path matched.
    """
    if door_id is not None:
        return Doors.objects.get(pk=door_id)

    if interlock_id is not None:
        return Interlock.objects.get(pk=interlock_id)

    return MemberbucksDevice.objects.get(pk=memberbucks_device_id)


class AccessSystemStatus(APIView):
    """
    get: This method returns the current status of the access system.
    """

    permission_classes = (HasExternalAccessControlAPIKey | permissions.IsAdminUser,)

    #: (response key, model, Prometheus label) for each device group. The label
    #: for vending machines is the historical "spacebucksDevice", which does
    #: not match `device.type` and is what existing dashboards query.
    device_groups = (
        ("doors", Doors, "door"),
        ("interlocks", Interlock, "interlock"),
        ("memberbucksDevices", MemberbucksDevice, "spacebucksDevice"),
    )

    def get(self, request):
        statusObject = {}
        error_if_offline = request.GET.get("errorIfOffline", False)
        a_device_is_offline = False

        for key, model, metrics_label in self.device_groups:
            devices = []
            online_count, offline_count, locked_out_count = 0, 0, 0

            for device in model.objects.all():
                offline = device.get_unavailable()

                devices.append(
                    {
                        "id": device.id,
                        "name": device.name,
                        "lastSeen": device.last_seen,
                        "lockedOut": device.locked_out,
                        "offline": offline,
                    }
                )

                if offline:
                    offline_count += 1
                else:
                    online_count += 1

                if device.locked_out:
                    locked_out_count += 1

                # A device excluded from reporting still shows as offline, it
                # just does not fail the uptime check.
                if offline and device.report_online_status:
                    a_device_is_offline = True

            statusObject[key] = devices

            metrics.devices_total.labels(type=metrics_label).set(len(devices))
            metrics.devices_online_total.labels(type=metrics_label).set(online_count)
            metrics.devices_offline_total.labels(type=metrics_label).set(offline_count)
            metrics.devices_locked_out_total.labels(type=metrics_label).set(
                locked_out_count
            )

        if error_if_offline and a_device_is_offline:
            return Response(statusObject, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(statusObject)


class UserAccessPermissions(APIView):
    """
    get: This method returns the current user's access permissions.
    """

    def get(self, request):
        return Response(request.user.profile.get_access_permissions())


class DeviceAccessView(APIView):
    """Grant or revoke one member's access to one device.

    The device itself names the Profile relation that records per-member
    access, so this works for any device type that has one.
    """

    permission_classes = (permissions.IsAdminUser,)

    #: "add" or "remove" on that relation.
    action = None

    def put(self, request, user_id, door_id=None, interlock_id=None):
        member = User.objects.get(pk=user_id)
        device = get_device(door_id=door_id, interlock_id=interlock_id)

        relation = getattr(member.profile, device.profile_relation)
        getattr(relation, self.action)(device)
        member.profile.save()
        device.sync()

        return Response()


class AuthoriseDevice(DeviceAccessView):
    """
    put: This method authorises a member to access a door or interlock.
    """

    action = "add"


class RevokeDevice(DeviceAccessView):
    """
    put: This method revokes a member's access to a door or interlock.
    """

    action = "remove"


class DeviceCommandView(APIView):
    """Send one remote command to a door or interlock.

    Subclasses name the model method that sends the command and the one that
    records it in the device's event log. The command is passed the request so
    it can also record which admin asked for it.
    """

    permission_classes = (permissions.IsAdminUser,)

    #: Name of the model method that sends the command.
    command = None
    #: Name of the model method that records it against the device.
    audit = None

    def post(
        self, request, door_id=None, interlock_id=None, memberbucks_device_id=None
    ):
        device = get_device(
            door_id=door_id,
            interlock_id=interlock_id,
            memberbucks_device_id=memberbucks_device_id,
        )

        getattr(device, self.audit)()
        result = getattr(device, self.command)(request=request)

        return Response({"success": result})


class ExternalDeviceCommandView(DeviceCommandView):
    """A command that MAY also be invoked externally with an API key.

    The permission classes admit an API key, so the config flag is checked
    here as well: holding a key is not enough unless the space has opted in to
    third-party control.
    """

    permission_classes = (HasExternalAccessControlAPIKey | permissions.IsAdminUser,)

    def post(self, request, **kwargs):
        if not (config.ENABLE_DOOR_BUMP_API or request.user.is_authenticated):
            return Response(
                {"success": False, "error": "This API is disabled in the config."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return super().post(request, **kwargs)


class SyncDevice(DeviceCommandView):
    """
    post: This method will force sync the specified device.
    """

    command = "sync"
    audit = "log_force_sync"


class RebootDevice(DeviceCommandView):
    """
    post: This method will reboot the specified device.
    """

    command = "reboot"
    audit = "log_force_rebooted"


class BumpDoor(ExternalDeviceCommandView):
    """
    post: This method will 'bump' the specified door. Note this MAY be called externally with an API key.
    """

    command = "bump"
    audit = "log_force_bump"


class LockDevice(ExternalDeviceCommandView):
    """
    post: This method will 'lock' the specified device. Note this MAY be called externally with an API key.
    """

    command = "lock"
    audit = "log_force_lock"


class UnlockDevice(ExternalDeviceCommandView):
    """
    post: This method will 'unlock' the specified device. Note this MAY be called externally with an API key.
    """

    command = "unlock"
    audit = "log_force_unlock"
