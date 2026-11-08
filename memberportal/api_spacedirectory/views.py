from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework_api_key.permissions import HasAPIKey
from rest_framework.views import APIView
from constance import config
from .models import SpaceAPI, SpaceAPISensor, SpaceAPISensorProperties
from profile.models import Profile
from api_general.models import SiteSession
import json
import logging

logger = logging.getLogger("api_spacedirectory")

# SpaceAPISensor.value and SpaceAPISensorProperties.value are DecimalField(10, 3).
VALUE_PLACES = Decimal("0.001")
VALUE_MAX = Decimal(10) ** 7

TRUE_WORDS = {"true", "t", "yes", "on", "1"}
FALSE_WORDS = {"false", "f", "no", "off", "0"}


def get_space():
    """The one SpaceAPI row, created closed the first time anyone needs it.

    No migration creates it, so a fresh install that ticks "enable" would
    otherwise get a 500 until an administrator adds the row by hand.
    """
    space = SpaceAPI.objects.order_by("pk").first()
    if space is None:
        space = SpaceAPI.objects.create()
    return space


def json_list(setting_name):
    """A Constance setting that holds a JSON list; anything else is no list.

    These are free-text boxes in the admin, so a typo must cost the list, not
    the whole public document.
    """
    raw = getattr(config, setting_name)
    if not raw or not str(raw).strip():
        return []

    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = None

    if not isinstance(value, list):
        logger.error(f"{setting_name} is not a JSON list, ignoring it: {raw!r}")
        return []

    return value


def parse_bool(value, field):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False

    raise ValidationError(f"{field} must be true or false.")


def parse_value(value, field, nullable):
    """A reading as the column will hold it: three decimal places, 7 digits before.

    Not ``full_clean()``: Django turns a float into a Decimal at the column's
    ten significant digits, so a plain 21.3 arrives as 21.30000000 and fails
    the three-places check, though ``save()`` rounds it happily.
    """
    if value is None and nullable:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValidationError(f"{field} must be a number.")

    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValidationError(f"{field} must be a number, not {value!r}.")

    if not number.is_finite() or abs(number) >= VALUE_MAX:
        raise ValidationError(f"{field} {value!r} is out of range.")

    return number.quantize(VALUE_PLACES)


def apply_property(sensor, payload):
    if not isinstance(payload, dict) or not str(payload.get("name") or "").strip():
        raise ValidationError(f"{sensor.name}: every property needs a name.")

    name = payload["name"]
    label = f"{sensor.name}/{name}"
    prop = sensor.properties.filter(name=name).order_by("pk").first()

    if prop is None:
        missing = [key for key in ("value", "unit") if key not in payload]
        if missing:
            raise ValidationError(
                f"{label} is new, so it needs a {' and a '.join(missing)}."
            )
        prop = SpaceAPISensorProperties(sensor_id=sensor, name=name)

    if "value" in payload:
        prop.value = parse_value(payload["value"], f"{label} value", nullable=False)
    if "unit" in payload:
        prop.unit = payload["unit"]

    prop.full_clean(exclude=["value"])
    prop.save()


def apply_sensor(payload):
    """Create or update one sensor, found by name. Raises ValidationError."""
    if not isinstance(payload, dict):
        raise ValidationError("Each sensor must be an object.")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("Every sensor needs a name.")

    sensor = SpaceAPISensor.objects.filter(name=name).order_by("pk").first()
    if sensor is None:
        if "type" not in payload:
            raise ValidationError(f"{name} is new, so it needs a type.")
        sensor = SpaceAPISensor(name=name)

    for key, attribute in (
        ("type", "sensor_type"),
        ("unit", "unit"),
        ("location", "location"),
        ("description", "description"),
    ):
        if key in payload:
            setattr(sensor, attribute, payload[key])

    if "value" in payload:
        sensor.value = parse_value(payload["value"], f"{name} value", nullable=True)

    sensor.full_clean(exclude=["value"])
    sensor.save()

    properties = payload.get("properties", [])
    if not isinstance(properties, list):
        raise ValidationError(f"{name}: properties must be a list.")
    for prop in properties:
        apply_property(sensor, prop)


class SpaceDirectoryStatus(APIView):
    """Generates a spaceapi compliant status message if enabled."""

    permission_classes = (permissions.AllowAny,)

    def get(self, request):
        if not config.ENABLE_SPACE_DIRECTORY:
            return Response(
                "Space Directory is not enabled on this server.",
                status=status.HTTP_404_NOT_FOUND,
            )

        else:
            spaceapi = {
                "space": config.SITE_OWNER,
                "logo": config.SITE_LOGO,
                "url": config.MAIN_SITE_URL,
                "contact": {
                    "email": config.SPACE_DIRECTORY_CONTACT_EMAIL,
                    "twitter": config.SPACE_DIRECTORY_CONTACT_TWITTER,
                    "phone": config.SPACE_DIRECTORY_CONTACT_PHONE,
                    "facebook": config.SPACE_DIRECTORY_CONTACT_FACEBOOK,
                },
                "spacefed": {
                    "spacenet": config.SPACE_DIRECTORY_FED_SPACENET,
                    "spacesaml": config.SPACE_DIRECTORY_FED_SPACESAML,
                    "spacephone": config.SPACE_DIRECTORY_FED_SPACEPHONE,
                },
                "projects": json_list("SPACE_DIRECTORY_PROJECTS"),
                "issue_report_channels": ["email"],
            }

            # Get the default data
            spaceapi_data = get_space()

            # Get a list of all the sensors
            spaceapi_sensors = SpaceAPISensor.objects.all()

            # Create an empty dict to add the sensor data to
            sensor_data = {}

            # Iterate over the sensors and update the dict as appropriate
            for sensor in spaceapi_sensors:
                sensor_details = {}
                # Do we already have a sensor of this type? If not, create it now
                if sensor.sensor_type not in sensor_data:
                    sensor_data[sensor.sensor_type] = []

                ## Setup the basic details
                sensor_details = {
                    "name": sensor.name,
                    "description": sensor.description or "",
                    "location": sensor.location,
                }

                ### Do we have properties? If so, let's add them
                if len(sensor.properties.all()) > 0:
                    sensor_details["properties"] = {}
                    for prop in sensor.properties.all():
                        properties = {
                            prop.name: {"value": prop.value, "unit": prop.unit}
                        }
                        sensor_details["properties"].update(properties)
                else:
                    sensor_details.update({"value": sensor.value, "unit": sensor.unit})

                sensor_data[sensor.sensor_type].append(sensor_details)

            ## Add the user count and members on site count to the sensors
            spaceapi_user_count = Profile.objects.all().filter(state="active").count()
            spaceapi_members_on_site = SiteSession.objects.filter(
                signout_date=None
            ).order_by("-signin_date")

            sensor_data["total_member_count"] = [{"value": spaceapi_user_count}]

            sensor_data["people_now_present"] = [
                {"value": spaceapi_members_on_site.count()}
            ]

            # Is the camera array empty? If not, add them
            cameras = json_list("SPACE_DIRECTORY_CAMS")
            if cameras:
                spaceapi["cameras"] = cameras

            # Set the STATE part of the schema, the icons, and the schema version
            spaceapi["state"] = {
                "open": spaceapi_data.space_is_open,
                "message": spaceapi_data.space_message,
                "lastchange": spaceapi_data.status_last_change.timestamp(),
            }
            spaceapi["icon"] = {
                "open": config.SPACE_DIRECTORY_ICON_OPEN,
                "closed": config.SPACE_DIRECTORY_ICON_CLOSED,
            }
            spaceapi["api_compatibility"] = ["14"]

            ## Add the sensor data to the main body of the schema
            spaceapi["sensors"] = sensor_data

            ## Add the location data based on the values in Constance
            spaceapi["location"] = {
                "address": config.SPACE_DIRECTORY_LOCATION_ADDRESS,
                "lat": config.SPACE_DIRECTORY_LOCATION_LAT,
                "lon": config.SPACE_DIRECTORY_LOCATION_LON,
            }

            # Return the JSON document
            return Response(spaceapi)


class SpaceDirectoryUpdate(APIView):
    """Allows authenticated users to update the SpaceAPI information"""

    permission_classes = (permissions.IsAdminUser | HasAPIKey,)

    def post(self, request):
        data = request.data
        if not any(key in data for key in ("is_open", "message", "sensors")):
            return Response(
                {
                    "error": "Nothing to update: send is_open, message or sensors "
                    "as a JSON body with Content-Type: application/json."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # All or nothing: a request that fails half way changes nothing.
            with transaction.atomic():
                current_status = get_space()
                changed = False

                if "is_open" in data:
                    is_open = parse_bool(data["is_open"], "is_open")
                    changed |= current_status.space_is_open != is_open
                    current_status.space_is_open = is_open

                if "message" in data:
                    message = data["message"]
                    if message is not None and not isinstance(message, str):
                        raise ValidationError("message must be text or null.")
                    changed |= current_status.space_message != message
                    current_status.space_message = message

                if "sensors" in data:
                    sensors = data["sensors"]
                    if not isinstance(sensors, list):
                        raise ValidationError("sensors must be a list.")
                    for sensor in sensors:
                        apply_sensor(sensor)

                # status_last_change is auto_now, so saving moves it. Only the
                # open state and the message are the space's state; a sensor
                # reading every minute is not a change of it.
                if changed:
                    current_status.full_clean()
                    current_status.save()

        except ValidationError as e:
            return Response(
                {"error": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response()
