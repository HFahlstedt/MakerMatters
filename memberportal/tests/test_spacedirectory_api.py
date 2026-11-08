"""Characterisation: the SpaceAPI directory in ``api_spacedirectory``.

SpaceAPI (spaceapi.io) is the format hackspaces publish their open/closed
state in, so that directories and phone apps can show it. Two endpoints:

* ``GET /api/spacedirectory/`` builds the public document from Constance
  settings, the single ``SpaceAPI`` row (open/closed and a message), the
  ``SpaceAPISensor`` rows, and two numbers computed live: active members and
  people signed in to the site. It is unauthenticated by design.
* ``POST /api/spacedirectory/update`` lets staff, or an integration holding an
  API key, set the state and create or update sensors. A door controller or a
  weather station is the usual caller, and it will not be a person reading
  error messages -- which is why so many tests below are about a request that
  fails half way, or does nothing and says 200.

``docs/SPACEDIRECTORY.md`` is the only specification of the update format, so
the tests treat its examples as the contract.
"""

import json
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

pytestmark = pytest.mark.django_db

URL = "/api/spacedirectory/"
UPDATE = "/api/spacedirectory/update"

DOCUMENT_KEYS = {
    "space",
    "logo",
    "url",
    "contact",
    "spacefed",
    "projects",
    "issue_report_channels",
    "state",
    "icon",
    "api_compatibility",
    "sensors",
    "location",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@pytest.fixture
def space(set_config):
    """An enabled directory with its one ``SpaceAPI`` row.

    No migration creates the row; an administrator has to add it in the Django
    admin. The tests that need it absent do not ask for this fixture.
    """
    from api_spacedirectory.models import SpaceAPI

    set_config(ENABLE_SPACE_DIRECTORY=True)
    return SpaceAPI.objects.create()


def make_sensor(**fields):
    from api_spacedirectory.models import SpaceAPISensor

    values = {
        "sensor_type": "temperature",
        "name": "workshop",
        "location": "main room",
        "description": "by the bench",
        "unit": "C",
        "value": 21.5,
    }
    values.update(fields)
    return SpaceAPISensor.objects.create(**values)


def make_property(sensor, name="gust", value=5, unit="m/s"):
    from api_spacedirectory.models import SpaceAPISensorProperties

    return SpaceAPISensorProperties.objects.create(
        sensor_id=sensor, name=name, value=value, unit=unit
    )


def document(client):
    response = client.get(URL)
    assert response.status_code == 200
    return json.loads(response.content)


def temperature(**overrides):
    """A new-sensor payload as the documentation's first example writes it."""
    sensor = {
        "type": "temperature",
        "name": "test_sensor",
        "location": "default",
        "description": "This is a sensor",
        "unit": "°C",
        "value": 21.0,
    }
    sensor.update(overrides)
    return sensor


def wind(properties, **overrides):
    sensor = {
        "type": "wind",
        "name": "wind_sensor_01",
        "location": "The Roof",
        "description": "A weather station",
        "properties": properties,
    }
    sensor.update(overrides)
    return sensor


class RecordingLogger:
    def __init__(self):
        self.errors = []

    def error(self, message, *args, **kwargs):
        self.errors.append(message)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def age(space):
    """Push the last change a day into the past, so that a bump is visible."""
    from api_spacedirectory.models import SpaceAPI

    SpaceAPI.objects.filter(pk=space.pk).update(
        status_last_change=timezone.now() - timedelta(days=1)
    )
    space.refresh_from_db()
    return space.status_last_change


def sensors_named(name):
    from api_spacedirectory.models import SpaceAPISensor

    return SpaceAPISensor.objects.filter(name=name)


# --------------------------------------------------------------------------
# GET: whether and for whom
# --------------------------------------------------------------------------


def test_the_directory_is_off_by_default_and_says_so(api_client, space, set_config):
    set_config(ENABLE_SPACE_DIRECTORY=False)

    response = api_client.get(URL)

    assert response.status_code == 404
    assert response.data == "Space Directory is not enabled on this server."


def test_the_directory_is_public(api_client, space):
    """No login and no key: this is what directories and phone apps poll."""
    assert api_client.get(URL).status_code == 200


def test_only_get_is_allowed_on_the_public_endpoint(api_client, space):
    assert api_client.post(URL, {}, format="json").status_code == 405


# --------------------------------------------------------------------------
# GET: the document
# --------------------------------------------------------------------------


def test_the_document_has_exactly_these_top_level_keys(api_client, space):
    assert set(document(api_client)) == DOCUMENT_KEYS


def test_the_document_is_built_from_constance(api_client, space, set_config):
    set_config(
        SITE_OWNER="Kalmar Makerspace",
        SITE_LOGO="https://example.test/logo.png",
        MAIN_SITE_URL="https://example.test",
        SPACE_DIRECTORY_CONTACT_EMAIL="hello@example.test",
        SPACE_DIRECTORY_CONTACT_TWITTER="@kalmar",
        SPACE_DIRECTORY_CONTACT_PHONE="+46700000000",
        SPACE_DIRECTORY_CONTACT_FACEBOOK="https://facebook.test/kalmar",
        SPACE_DIRECTORY_FED_SPACENET=True,
        SPACE_DIRECTORY_PROJECTS='["https://wiki.example.test"]',
        SPACE_DIRECTORY_ICON_OPEN="https://example.test/open.png",
        SPACE_DIRECTORY_ICON_CLOSED="https://example.test/closed.png",
        SPACE_DIRECTORY_LOCATION_ADDRESS="1 Verkstadsgatan",
        SPACE_DIRECTORY_LOCATION_LAT=56.66,
        SPACE_DIRECTORY_LOCATION_LON=16.36,
    )

    body = document(api_client)

    assert body["space"] == "Kalmar Makerspace"
    assert body["logo"] == "https://example.test/logo.png"
    assert body["url"] == "https://example.test"
    assert body["contact"] == {
        "email": "hello@example.test",
        "twitter": "@kalmar",
        "phone": "+46700000000",
        "facebook": "https://facebook.test/kalmar",
    }
    assert body["spacefed"] == {
        "spacenet": True,
        "spacesaml": False,
        "spacephone": False,
    }
    assert body["projects"] == ["https://wiki.example.test"]
    assert body["icon"] == {
        "open": "https://example.test/open.png",
        "closed": "https://example.test/closed.png",
    }
    assert body["location"] == {
        "address": "1 Verkstadsgatan",
        "lat": 56.66,
        "lon": 16.36,
    }
    assert body["api_compatibility"] == ["14"]
    assert body["issue_report_channels"] == ["email"]


def test_the_state_comes_from_the_spaceapi_row_and_starts_closed(api_client, space):
    body = document(api_client)

    assert body["state"] == {
        "open": False,
        "message": None,
        "lastchange": space.status_last_change.timestamp(),
    }


def test_an_open_space_reports_its_message(api_client, space):
    space.space_is_open = True
    space.space_message = "Open night"
    space.save()
    space.refresh_from_db()

    assert document(api_client)["state"] == {
        "open": True,
        "message": "Open night",
        "lastchange": space.status_last_change.timestamp(),
    }


def test_the_open_and_message_constance_settings_are_never_read(
    api_client, space, set_config
):
    """Pinned as behaviour: two settings the admin page offers do nothing.

    ``SPACE_DIRECTORY_OPEN`` and ``SPACE_DIRECTORY_MESSAGE`` sit in the
    "Space Directory" group of the Constance admin, but the state is read from
    the ``SpaceAPI`` row. Removing them changes the Constance golden file, so
    whether to is a decision rather than a patch.
    """
    set_config(SPACE_DIRECTORY_OPEN=True, SPACE_DIRECTORY_MESSAGE="Come on in")

    state = document(api_client)["state"]

    assert state["open"] is False
    assert state["message"] is None


def test_member_and_presence_counts_are_added_as_sensors(
    api_client, space, make_member
):
    from api_general.models import SiteSession

    here = make_member(state="active", rfid="SD-1")
    make_member(state="active", rfid="SD-2")
    make_member(state="inactive", rfid="SD-3")
    make_member(state="noob", rfid="SD-4")
    SiteSession.objects.create(user=here.user)
    SiteSession.objects.create(user=here.user, signout_date=here.created)

    sensors = document(api_client)["sensors"]

    assert sensors["total_member_count"] == [{"value": 2}]
    assert sensors["people_now_present"] == [{"value": 1}]


def test_sensors_are_grouped_by_type(api_client, space):
    make_sensor(name="bench", location="main room", value=21.5, unit="C")
    make_sensor(name="loft", description=None, location=None, value=19, unit="C")
    make_sensor(sensor_type="humidity", name="air", value=40, unit="%")

    sensors = document(api_client)["sensors"]

    assert sensors["temperature"] == [
        {
            "name": "bench",
            "description": "by the bench",
            "location": "main room",
            "value": 21.5,
            "unit": "C",
        },
        # A missing description is "", a missing location stays null.
        {
            "name": "loft",
            "description": "",
            "location": None,
            "value": 19,
            "unit": "C",
        },
    ]
    assert sensors["humidity"][0]["value"] == 40


def test_a_sensor_with_properties_lists_them_instead_of_a_value(api_client, space):
    anemometer = make_sensor(
        sensor_type="wind", name="roof", value=None, unit=None, location="roof"
    )
    make_property(anemometer, "speed", 5, "m/s")
    make_property(anemometer, "gust", 9.5, "m/s")

    (sensor,) = document(api_client)["sensors"]["wind"]

    assert sensor["properties"] == {
        "speed": {"value": 5, "unit": "m/s"},
        "gust": {"value": 9.5, "unit": "m/s"},
    }
    assert "value" not in sensor and "unit" not in sensor


def test_sensor_values_are_json_numbers_not_strings(api_client, space):
    """SpaceAPI requires numbers; a DecimalField could have emitted "21.500"."""
    make_sensor(value=21.5)

    (sensor,) = document(api_client)["sensors"]["temperature"]

    assert sensor["value"] == 21.5 and not isinstance(sensor["value"], str)


# --------------------------------------------------------------------------
# GET: defects
# --------------------------------------------------------------------------


def test_enabling_the_directory_on_a_fresh_install_creates_a_closed_space(
    api_client, set_config
):
    """Used to be a 500: a fresh install could not use the feature it enabled.

    No migration creates the ``SpaceAPI`` row, and the view did
    ``SpaceAPI.objects.get()``. Ticking "Enable Space Directory" in Constance
    turned a 404 into a 500 until an administrator happened to add the row in
    the Django admin. It is created on first use now, closed -- the model's
    own comment says a space must never show as open by accident.
    """
    from api_spacedirectory.models import SpaceAPI

    set_config(ENABLE_SPACE_DIRECTORY=True)
    assert not SpaceAPI.objects.exists()

    assert document(api_client)["state"]["open"] is False
    document(api_client)

    assert SpaceAPI.objects.count() == 1


def test_cameras_are_published_when_a_list_is_configured(api_client, space, set_config):
    """Used to be inverted: the key appeared only when the setting was empty.

    ``if not config.SPACE_DIRECTORY_CAMS`` added it for a blank setting, as
    ``""``, and never for the default ``"[]"`` or a real list. The value was
    also the raw string, where ``projects`` was run through ``json.loads``.
    """
    assert "cameras" not in document(api_client)  # the shipped default, "[]"

    set_config(SPACE_DIRECTORY_CAMS='["https://example.test/cam.jpg"]')
    assert document(api_client)["cameras"] == ["https://example.test/cam.jpg"]

    set_config(SPACE_DIRECTORY_CAMS="")
    assert "cameras" not in document(api_client)


@pytest.mark.parametrize(
    "bad", ["https://wiki.example.test", '{"a": 1}', "[unterminated", "null"]
)
def test_a_projects_setting_that_is_not_a_json_list_is_ignored_and_logged(
    api_client, space, set_config, monkeypatch, bad
):
    """Used to be a 500: ``json.loads(config.SPACE_DIRECTORY_PROJECTS)`` was bare.

    The setting is a free-text box in the Constance admin, so a typo there took
    the whole public document down rather than just the projects list.
    """
    import api_spacedirectory.views as views

    log = RecordingLogger()
    monkeypatch.setattr(views, "logger", log)
    set_config(SPACE_DIRECTORY_PROJECTS=bad)

    body = document(api_client)

    assert body["projects"] == []
    assert len(log.errors) == 1 and "SPACE_DIRECTORY_PROJECTS" in log.errors[0]


# --------------------------------------------------------------------------
# POST: who may update
# --------------------------------------------------------------------------


def test_updating_needs_staff_or_an_api_key(
    api_client, as_member, as_admin, with_api_key, make_member, space
):
    assert api_client.post(UPDATE, {"message": "x"}, format="json").status_code in (
        401,
        403,
    )

    member = make_member(state="active", rfid="SD-PERM")
    assert (
        as_member(member).post(UPDATE, {"message": "x"}, format="json").status_code
        == 403
    )
    api_client.force_authenticate(user=None)

    assert (
        as_admin().post(UPDATE, {"message": "staff"}, format="json").status_code == 200
    )
    api_client.force_authenticate(user=None)

    assert (
        with_api_key().post(UPDATE, {"message": "key"}, format="json").status_code
        == 200
    )


def test_the_update_endpoint_answers_post_only(as_admin, space):
    assert as_admin().get(UPDATE).status_code == 405


# --------------------------------------------------------------------------
# POST: state
# --------------------------------------------------------------------------


def test_the_state_and_message_can_be_set(as_admin, space):
    response = as_admin().post(
        UPDATE, {"is_open": True, "message": "Open night"}, format="json"
    )

    assert response.status_code == 200
    space.refresh_from_db()
    assert (space.space_is_open, space.space_message) == (True, "Open night")


def test_a_field_that_is_not_sent_is_left_alone(as_admin, space):
    space.space_is_open = True
    space.space_message = "Open night"
    space.save()

    as_admin().post(UPDATE, {"message": "Closing soon"}, format="json")

    space.refresh_from_db()
    assert (space.space_is_open, space.space_message) == (True, "Closing soon")


# --------------------------------------------------------------------------
# POST: sensors
# --------------------------------------------------------------------------


def test_a_new_sensor_is_created_from_the_documented_payload(as_admin, space):
    response = as_admin().post(UPDATE, {"sensors": [temperature()]}, format="json")

    assert response.status_code == 200
    sensor = sensors_named("test_sensor").get()
    assert (sensor.sensor_type, sensor.location, sensor.description) == (
        "temperature",
        "default",
        "This is a sensor",
    )
    assert (float(sensor.value), sensor.unit) == (21.0, "°C")


def test_a_new_sensor_with_properties_creates_them_too(as_admin, space):
    properties = [
        {"name": "speed", "unit": "m/s", "value": 5.0},
        {"name": "gust", "unit": "m/s", "value": 9.0},
    ]

    as_admin().post(UPDATE, {"sensors": [wind(properties)]}, format="json")

    sensor = sensors_named("wind_sensor_01").get()
    assert {(p.name, float(p.value), p.unit) for p in sensor.properties.all()} == {
        ("speed", 5.0, "m/s"),
        ("gust", 9.0, "m/s"),
    }


def test_an_existing_sensor_is_updated_in_place_by_name(as_admin, space):
    make_sensor(name="test_sensor", value=10, unit="C", location="old")

    as_admin().post(
        UPDATE, {"sensors": [{"name": "test_sensor", "value": 22.5}]}, format="json"
    )

    sensor = sensors_named("test_sensor").get()
    # Only what was sent changes: "if it exists but the value is not included
    # or changed, it will not be updated".
    assert (float(sensor.value), sensor.unit, sensor.location) == (22.5, "C", "old")


def test_an_existing_sensors_other_fields_can_each_be_changed(as_admin, space):
    make_sensor(name="test_sensor")

    as_admin().post(
        UPDATE,
        {
            "sensors": [
                {
                    "name": "test_sensor",
                    "type": "humidity",
                    "unit": "%",
                    "location": "loft",
                    "description": "moved",
                }
            ]
        },
        format="json",
    )

    sensor = sensors_named("test_sensor").get()
    assert (sensor.sensor_type, sensor.unit, sensor.location, sensor.description) == (
        "humidity",
        "%",
        "loft",
        "moved",
    )


def test_an_existing_sensors_property_is_updated_and_a_new_one_added(as_admin, space):
    from api_spacedirectory.models import SpaceAPISensorProperties

    anemometer = make_sensor(sensor_type="wind", name="roof", value=None)
    make_property(anemometer, "speed", 1, "m/s")

    as_admin().post(
        UPDATE,
        {
            "sensors": [
                {
                    "name": "roof",
                    "properties": [
                        {"name": "speed", "value": 6.5, "unit": "m/s"},
                        {"name": "gust", "value": 9, "unit": "m/s"},
                    ],
                }
            ]
        },
        format="json",
    )

    assert {
        (p.name, float(p.value)) for p in SpaceAPISensorProperties.objects.all()
    } == {("speed", 6.5), ("gust", 9.0)}


def test_a_sensor_is_identified_by_its_name_alone(as_admin, space):
    """Pinned as behaviour -- and the documentation's own example trips on it.

    The multi-sensor example sends a temperature sensor and a humidity sensor
    that are both called ``test_sensor``. The second matches the first by name
    and rewrites its type, so one sensor remains and it is a humidity sensor
    reading 45.
    """
    as_admin().post(
        UPDATE,
        {
            "sensors": [
                temperature(),
                temperature(type="humidity", unit="%H", value=45.0),
            ]
        },
        format="json",
    )

    sensor = sensors_named("test_sensor").get()
    assert (sensor.sensor_type, float(sensor.value)) == ("humidity", 45.0)


def test_a_sensor_report_does_not_move_the_last_change_time(as_admin, space):
    """Used to move on every POST.

    ``status_last_change`` is ``auto_now`` and the view always saved the row,
    so a weather station reporting every minute reported the space as having
    "changed" every minute -- the one field directories use to say how long it
    has been open or closed. It is saved only when the state changed now.
    """
    before = age(space)

    as_admin().post(UPDATE, {"sensors": [temperature()]}, format="json")

    space.refresh_from_db()
    assert space.status_last_change == before


def test_repeating_the_current_state_does_not_move_the_last_change_time(
    as_admin, space
):
    space.space_is_open = True
    space.space_message = "Open"
    space.save()
    before = age(space)

    as_admin().post(UPDATE, {"is_open": True, "message": "Open"}, format="json")

    space.refresh_from_db()
    assert space.status_last_change == before


@pytest.mark.parametrize("body", [{"is_open": True}, {"message": "Back at 8"}])
def test_changing_the_open_state_or_the_message_moves_the_last_change_time(
    as_admin, space, body
):
    before = age(space)

    as_admin().post(UPDATE, body, format="json")

    space.refresh_from_db()
    assert space.status_last_change > before


# --------------------------------------------------------------------------
# POST: what is accepted that should not be, and what fails half way
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        (1, True),
        ("false", False),
        ("no", False),
        (0, False),
    ],
)
def test_is_open_accepts_quoted_and_numeric_booleans(as_admin, space, value, expected):
    """Used to raise at save time for "true"/"false": JSON ``true`` worked, but
    a form-encoded client, or anything that quotes its booleans, sent a string
    and ``BooleanField.to_python`` accepts only the Python spellings."""
    space.space_is_open = not expected
    space.save()

    response = as_admin().post(UPDATE, {"is_open": value}, format="json")

    assert response.status_code == 200
    space.refresh_from_db()
    assert space.space_is_open is expected


@pytest.mark.parametrize("value", ["maybe", "", 2, None, [], {"a": 1}])
def test_is_open_that_is_not_a_boolean_is_a_400_and_changes_nothing(
    as_admin, space, value
):
    space.space_message = "keep"
    space.save()

    response = as_admin().post(
        UPDATE, {"is_open": value, "message": "changed"}, format="json"
    )

    assert response.status_code == 400
    assert "is_open" in response.data["error"]
    space.refresh_from_db()
    assert (space.space_is_open, space.space_message) == (False, "keep")


def test_the_documented_curl_command_without_a_content_type_is_refused_with_a_hint(
    as_admin, space
):
    """Used to do nothing and answer 200.

    ``curl -d '{"is_open": true, ...}'`` posts as
    ``application/x-www-form-urlencoded`` unless told otherwise, so DRF read
    the JSON text as one odd form key. None of ``is_open``, ``message`` or
    ``sensors`` was present, the view took that for an update of nothing, and
    whoever followed the documentation got a space that stayed closed. It is a
    400 that says what to send now, and the documentation names the header.
    """
    response = as_admin().post(
        UPDATE,
        data='{"is_open": true, "message": "Open Night TONIGHT!"}',
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 400
    assert "Content-Type: application/json" in response.data["error"]
    space.refresh_from_db()
    assert (space.space_is_open, space.space_message) == (False, None)


def test_the_curl_command_works_with_the_content_type_the_docs_now_give(
    as_admin, space
):
    response = as_admin().post(
        UPDATE,
        data='{"is_open": true, "message": "Open Night TONIGHT!"}',
        content_type="application/json",
    )

    assert response.status_code == 200
    space.refresh_from_db()
    assert (space.space_is_open, space.space_message) == (True, "Open Night TONIGHT!")


@pytest.mark.parametrize("body", [{}, {"unrelated": 1}, []])
def test_an_update_that_names_nothing_to_update_is_a_400(as_admin, space, body):
    assert as_admin().post(UPDATE, body, format="json").status_code == 400


@pytest.mark.parametrize("message", [5, ["a"], {"a": 1}, "x" * 256])
def test_a_message_that_is_not_text_or_is_too_long_is_a_400(as_admin, space, message):
    response = as_admin().post(UPDATE, {"message": message}, format="json")

    assert response.status_code == 400
    space.refresh_from_db()
    assert space.space_message is None


def test_a_message_can_be_cleared_with_null(as_admin, space):
    space.space_message = "Open night"
    space.save()

    as_admin().post(UPDATE, {"message": None}, format="json")

    space.refresh_from_db()
    assert space.space_message is None


def test_updating_on_a_fresh_install_creates_the_row(as_admin, set_config):
    from api_spacedirectory.models import SpaceAPI

    response = as_admin().post(UPDATE, {"message": "hello"}, format="json")

    assert response.status_code == 200
    assert SpaceAPI.objects.get().space_message == "hello"


@pytest.mark.parametrize("omitted", ["location", "description"])
def test_a_new_sensor_needs_only_a_name_and_a_type(
    api_client, as_admin, space, omitted
):
    """Used to be a KeyError: required by the code, optional in the model and docs.

    ``SpaceAPISensor.location`` and ``.description`` are nullable and the
    documentation never says a sensor needs either, but the create path read
    ``sensor["location"]`` and ``sensor["description"]`` regardless, so a first
    report without them was a 500 and stored nothing.
    """
    payload = temperature()
    del payload[omitted]

    response = as_admin().post(UPDATE, {"sensors": [payload]}, format="json")

    assert response.status_code == 200
    assert getattr(sensors_named("test_sensor").get(), omitted) is None
    api_client.force_authenticate(user=None)
    (published,) = document(api_client)["sensors"]["temperature"]
    # The public document shows a missing description as "", a missing
    # location as null.
    assert published[omitted] == ("" if omitted == "description" else None)


def test_a_new_sensor_without_a_type_is_a_400(as_admin, space):
    payload = temperature()
    del payload["type"]

    response = as_admin().post(UPDATE, {"sensors": [payload]}, format="json")

    assert response.status_code == 400
    assert "type" in response.data["error"]
    assert not sensors_named("test_sensor").exists()


@pytest.mark.parametrize("name", [None, "", "   ", 5, ["a"]])
def test_a_sensor_without_a_usable_name_is_a_400(as_admin, space, name):
    from api_spacedirectory.models import SpaceAPISensor

    response = as_admin().post(
        UPDATE, {"sensors": [temperature(name=name)]}, format="json"
    )

    assert response.status_code == 400
    assert not SpaceAPISensor.objects.exists()


def test_a_sensor_type_outside_the_declared_choices_is_a_400(as_admin, space):
    """Used to be stored, then published as a key in the public document.

    SpaceAPI defines a fixed vocabulary for ``sensors``; "banana" went out as
    is, for new sensors and for a type change on an existing one alike.
    """
    make_sensor(name="bench")

    new = as_admin().post(
        UPDATE, {"sensors": [temperature(type="banana")]}, format="json"
    )
    changed = as_admin().post(
        UPDATE, {"sensors": [{"name": "bench", "type": "banana"}]}, format="json"
    )

    assert (new.status_code, changed.status_code) == (400, 400)
    assert not sensors_named("test_sensor").exists()
    assert sensors_named("bench").get().sensor_type == "temperature"


@pytest.mark.parametrize(
    "sent, stored",
    [
        (21.3, "21.300"),
        (0.1, "0.100"),
        ("19.12345", "19.123"),
        (7, "7.000"),
        ("9999999.999", "9999999.999"),
    ],
)
def test_readings_are_stored_to_three_decimal_places(as_admin, space, sent, stored):
    """Guards the obvious fix going wrong: ``full_clean()`` rejects a plain 21.3
    (Django makes a ten-digit Decimal of the float and then counts its places),
    though ``save()`` has always rounded it. Real sensors send such readings."""
    response = as_admin().post(
        UPDATE, {"sensors": [temperature(value=sent)]}, format="json"
    )

    assert response.status_code == 200
    assert str(sensors_named("test_sensor").get().value) == stored


def test_a_reading_can_be_cleared_with_null(as_admin, space):
    make_sensor(name="test_sensor", value=10)

    as_admin().post(
        UPDATE, {"sensors": [{"name": "test_sensor", "value": None}]}, format="json"
    )

    assert sensors_named("test_sensor").get().value is None


@pytest.mark.parametrize(
    "value", ["warm", "NaN", "Infinity", 10**7, [], {"a": 1}, True]
)
def test_a_reading_that_is_not_a_usable_number_is_a_400_and_changes_nothing(
    as_admin, space, value
):
    """Used to be a 500 that blamed the wrong field.

    The update ran inside ``try: ... except Exception:`` and the handler was
    the *create* path. A bad value on an existing sensor failed at ``save()``,
    the handler tried to create a second sensor, and reported that
    ``sensor["location"]`` was missing -- which was not what was wrong.
    """
    make_sensor(name="test_sensor", value=10)

    response = as_admin().post(
        UPDATE, {"sensors": [{"name": "test_sensor", "value": value}]}, format="json"
    )

    assert response.status_code == 400
    assert "test_sensor value" in response.data["error"]
    assert float(sensors_named("test_sensor").get().value) == 10


def test_a_bad_property_on_an_existing_sensor_is_a_400_and_duplicates_nothing(
    as_admin, space
):
    """Used to create a duplicate sensor before raising.

    A property value that did not validate failed inside the update path. The
    inner bare ``except:`` tried to create it as new and failed again, and the
    outer ``except Exception`` fell back to creating the whole *sensor*. With
    ``location`` and ``description`` in the request that succeeded, so the
    sensor existed twice under one name before the final error.
    """
    from api_spacedirectory.models import SpaceAPISensorProperties

    anemometer = make_sensor(sensor_type="wind", name="roof", value=None)
    make_property(anemometer, "speed", 1, "m/s")

    response = as_admin().post(
        UPDATE,
        {
            "sensors": [
                wind([{"name": "speed", "value": "fast", "unit": "m/s"}], name="roof")
            ]
        },
        format="json",
    )

    assert response.status_code == 400
    assert sensors_named("roof").count() == 1
    assert float(SpaceAPISensorProperties.objects.get().value) == 1


def test_the_old_documentation_property_example_is_a_400_and_retries_add_nothing(
    as_admin, space
):
    """Used to grow the table by one sensor per retry.

    The docs gave each property a key named after itself (``"speed": 5.0``)
    where the code reads ``"value"``. The sensor was saved, the property
    raised, and every retry found the sensor, failed the same way, and the
    exception handler created another with the same name. The request is
    refused whole now -- nothing is stored, so a device that keeps retrying
    changes nothing -- and the documentation says ``value``.
    """
    payload = {"sensors": [wind([{"name": "gust", "unit": "m/s", "speed": 5.0}])]}

    for _attempt in range(3):
        response = as_admin().post(UPDATE, payload, format="json")
        assert response.status_code == 400
        assert "value" in response.data["error"]

    assert not sensors_named("wind_sensor_01").exists()


@pytest.mark.parametrize(
    "properties, mentions",
    [
        ([{"name": "gust", "value": 1}], "unit"),
        ([{"name": "gust", "unit": "m/s"}], "value"),
        ([{"value": 1, "unit": "m/s"}], "name"),
        (["gust"], "name"),
        ({"name": "gust"}, "list"),
    ],
)
def test_a_malformed_property_is_a_400_that_says_what_is_wrong(
    as_admin, space, properties, mentions
):
    response = as_admin().post(UPDATE, {"sensors": [wind(properties)]}, format="json")

    assert response.status_code == 400
    assert mentions in response.data["error"]
    assert not sensors_named("wind_sensor_01").exists()


@pytest.mark.parametrize(
    "sensors", [{"name": "test_sensor"}, "test_sensor", 5, [None], ["name"], [[1]]]
)
def test_sensors_that_are_not_a_list_of_objects_is_a_400(as_admin, space, sensors):
    """Used to be a TypeError: a dict was iterated as its keys."""
    response = as_admin().post(UPDATE, {"sensors": sensors}, format="json")

    assert response.status_code == 400


def test_a_request_that_fails_half_way_changes_nothing(as_admin, space):
    """The whole update is one transaction: state, message and every sensor."""
    make_sensor(name="bench", value=10)

    response = as_admin().post(
        UPDATE,
        {
            "is_open": True,
            "message": "Open",
            "sensors": [
                {"name": "bench", "value": 99},
                {"name": "fresh", "type": "banana"},
            ],
        },
        format="json",
    )

    assert response.status_code == 400
    space.refresh_from_db()
    assert (space.space_is_open, space.space_message) == (False, None)
    assert float(sensors_named("bench").get().value) == 10
    assert not sensors_named("fresh").exists()


def test_the_documentations_full_example_applies(as_admin, api_client, space):
    """The docs' last example, as corrected: unique names, ``value`` keys."""
    body = {
        "is_open": True,
        "message": "Soldering workshop tonight - 8pm to 10pm",
        "sensors": [
            temperature(),
            wind(
                [
                    {"name": "speed", "unit": "m/s", "value": 5.0},
                    {"name": "gust", "unit": "m/s", "value": 9.0},
                ]
            ),
        ],
    }

    assert as_admin().post(UPDATE, body, format="json").status_code == 200
    api_client.force_authenticate(user=None)

    published = document(api_client)
    assert published["state"]["open"] is True
    assert published["sensors"]["temperature"][0]["value"] == 21.0
    assert published["sensors"]["wind"][0]["properties"] == {
        "speed": {"value": 5.0, "unit": "m/s"},
        "gust": {"value": 9.0, "unit": "m/s"},
    }


def test_every_json_example_in_the_documentation_is_accepted(as_admin, space):
    """The docs are the only specification of the update format.

    Every ``json`` block in ``docs/SPACEDIRECTORY.md`` is posted as written.
    Before this, three of the five were wrong: one was not valid JSON, one gave
    two different sensors the same name, and the property examples used a key
    the code does not read.
    """
    import re
    from pathlib import Path

    docs = Path(__file__).resolve().parents[2] / "docs" / "SPACEDIRECTORY.md"
    if not docs.exists():  # the Docker image does not ship docs/
        pytest.skip("docs/SPACEDIRECTORY.md is not available")

    examples = re.findall(r"```json\n(.*?)```", docs.read_text(), re.S)
    assert len(examples) >= 5

    for example in examples:
        response = as_admin().post(
            UPDATE, data=example, content_type="application/json"
        )
        assert response.status_code == 200, example


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


def test_only_one_spaceapi_row_is_allowed_when_the_model_is_validated(space):
    """Enforced by ``clean()``, so by the admin form but not by ``save()``."""
    from api_spacedirectory.models import SpaceAPI

    with pytest.raises(ValidationError, match="only have one"):
        SpaceAPI().full_clean()


def test_the_models_describe_themselves(space):
    sensor = make_sensor(name="bench", location="main room")
    prop = make_property(make_sensor(sensor_type="wind", name="roof"), "gust")

    assert str(space).startswith("Currently CLOSED")
    space.space_is_open = True
    assert str(space).startswith("Currently OPEN")
    assert str(sensor) == "bench - main room (temperature)"
    assert str(prop) == "roof/gust"
