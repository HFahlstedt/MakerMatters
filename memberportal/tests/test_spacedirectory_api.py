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

import pytest
from django.core.exceptions import ValidationError

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


def as_committed():
    """Let the test keep querying after a ``save()`` raised.

    pytest-django wraps each test in a transaction, and Django marks it broken
    when a ``save()`` inside raises. Production has no such wrapper
    (``ATOMIC_REQUESTS`` is unset), so whatever the view wrote before the
    exception is already committed and the next query works. Clearing the flag
    reproduces that.
    """
    from django.db import connection

    connection.needs_rollback = False


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


def test_enabling_the_directory_without_a_spaceapi_row_is_a_500(api_client, set_config):
    """DEFECT, pinned: a fresh install cannot use the feature it just turned on.

    No migration creates the ``SpaceAPI`` row, and the view does
    ``SpaceAPI.objects.get()``. Ticking "Enable Space Directory" in Constance
    therefore turns a 404 into a 500 until an administrator happens to add
    the row in the Django admin.
    """
    from api_spacedirectory.models import SpaceAPI

    set_config(ENABLE_SPACE_DIRECTORY=True)

    with pytest.raises(SpaceAPI.DoesNotExist):
        api_client.get(URL)


def test_cameras_are_never_published_for_a_configured_list(
    api_client, space, set_config
):
    """DEFECT, pinned: the condition is inverted, and the value is a string.

    ``if not config.SPACE_DIRECTORY_CAMS: spaceapi["cameras"] = ...`` adds the
    key only when the setting is empty. The default ``"[]"`` is a non-empty
    string, and so is a real list of URLs, so neither emits it; only a blank
    setting does, as ``""``. And when it does fire the value is the raw string
    -- ``projects`` is run through ``json.loads``, ``cameras`` is not.
    """
    assert "cameras" not in document(api_client)

    set_config(SPACE_DIRECTORY_CAMS='["https://example.test/cam.jpg"]')
    assert "cameras" not in document(api_client)

    set_config(SPACE_DIRECTORY_CAMS="")
    assert document(api_client)["cameras"] == ""


def test_invalid_json_in_the_projects_setting_is_a_500(api_client, space, set_config):
    """DEFECT, pinned: ``json.loads(config.SPACE_DIRECTORY_PROJECTS)`` is bare.

    The setting is a free-text box in the Constance admin, so a typo there
    takes the whole public document down rather than just the projects list.
    """
    set_config(SPACE_DIRECTORY_PROJECTS="https://wiki.example.test")

    with pytest.raises(json.JSONDecodeError):
        api_client.get(URL)


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


def test_an_update_bumps_the_last_change_time_even_when_nothing_about_the_state_changed(
    as_admin, space
):
    """DEFECT, pinned: ``lastchange`` moves on every POST.

    ``status_last_change`` is ``auto_now`` and the view always calls
    ``current_status.save()``. A weather station reporting every minute
    therefore reports the space as having "changed" every minute, which is the
    one field directories use to say how long it has been open or closed.
    """
    before = space.status_last_change

    as_admin().post(UPDATE, {"sensors": [temperature()]}, format="json")

    space.refresh_from_db()
    assert space.status_last_change > before


# --------------------------------------------------------------------------
# POST: what is accepted that should not be, and what fails half way
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "false"])
def test_is_open_as_a_string_is_a_500(as_admin, space, value):
    """DEFECT, pinned: the value goes into a BooleanField unchecked.

    JSON ``true`` works. A form-encoded client, or anything that quotes its
    booleans, sends a string, and ``BooleanField.to_python`` accepts only
    ``"True"``/``"False"``/``"t"``/``"f"``/``"1"``/``"0"`` -- so ``"true"``
    raises when the row is saved, after the request has otherwise been
    accepted.
    """
    with pytest.raises(ValidationError):
        as_admin().post(UPDATE, {"is_open": value}, format="json")


def test_the_documented_curl_command_does_nothing_and_answers_200(as_admin, space):
    """DEFECT, pinned: the example in the docs sends the wrong content type.

    ``curl -d '{"is_open": true, ...}'`` posts as
    ``application/x-www-form-urlencoded`` unless told otherwise, so DRF reads
    the JSON text as one odd form key. None of ``is_open``, ``message`` or
    ``sensors`` is present, and the view treats that as an update of nothing:
    200, and no change. Whoever follows the documentation gets a space that
    stays closed.
    """
    response = as_admin().post(
        UPDATE,
        data='{"is_open": true, "message": "Open Night TONIGHT!"}',
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 200
    space.refresh_from_db()
    assert (space.space_is_open, space.space_message) == (False, None)


def test_updating_without_a_spaceapi_row_is_a_500(as_admin, set_config):
    from api_spacedirectory.models import SpaceAPI

    with pytest.raises(SpaceAPI.DoesNotExist):
        as_admin().post(UPDATE, {"message": "x"}, format="json")


@pytest.mark.parametrize("missing", ["location", "description", "type"])
def test_a_new_sensor_must_send_location_description_and_type(as_admin, space, missing):
    """DEFECT, pinned: required in the code, optional in the model and the docs.

    ``SpaceAPISensor.location`` and ``.description`` are nullable, and the
    documentation never says a sensor needs either. The create path reads
    ``sensor["location"]`` and ``sensor["description"]`` regardless, so a
    first report without them is a ``KeyError`` -- a 500 -- and nothing is
    stored.
    """
    payload = temperature()
    del payload[missing]

    with pytest.raises(KeyError):
        as_admin().post(UPDATE, {"sensors": [payload]}, format="json")

    assert not sensors_named("test_sensor").exists()


def test_a_sensor_type_outside_the_declared_choices_is_stored(as_admin, space):
    """DEFECT, pinned: choices are not enforced, and the document trusts them.

    The type becomes a key in the public document's ``sensors`` object, which
    SpaceAPI defines a fixed vocabulary for. "banana" is published as is.
    """
    as_admin().post(UPDATE, {"sensors": [temperature(type="banana")]}, format="json")

    assert sensors_named("test_sensor").get().sensor_type == "banana"


def test_a_sensors_value_that_is_not_a_number_is_a_500_that_looks_like_a_missing_field(
    as_admin, space
):
    """DEFECT, pinned: errors are used as control flow.

    The update runs inside ``try: ... except Exception:`` and the handler is
    the *create* path. A bad value on an existing sensor fails at ``save()``,
    the handler tries to create a second sensor, and reports that
    ``sensor["location"]`` is missing -- which is not what was wrong. The
    stored sensor is untouched.
    """
    make_sensor(name="test_sensor", value=10)

    with pytest.raises(KeyError):
        as_admin().post(
            UPDATE,
            {"sensors": [{"name": "test_sensor", "value": "warm"}]},
            format="json",
        )

    as_committed()
    assert float(sensors_named("test_sensor").get().value) == 10


@pytest.mark.django_db(transaction=True)
def test_a_bad_property_on_an_existing_sensor_creates_a_duplicate_sensor(
    as_admin, space
):
    """DEFECT, pinned: the same handler, with a visible casualty.

    A property value that does not validate fails inside the update path. The
    inner bare ``except:`` tries to create it as new and fails again, and the
    outer ``except Exception`` falls back to creating the whole *sensor*. When
    the request carries ``location`` and ``description`` that succeeds, so the
    sensor now exists twice under one name before the final error is raised.

    Runs outside the usual test transaction: inside it, the failed ``save()``
    would poison the fallback's queries, which production's autocommit does not.
    """
    anemometer = make_sensor(sensor_type="wind", name="roof", value=None)
    make_property(anemometer, "speed", 1, "m/s")

    with pytest.raises(ValidationError):
        as_admin().post(
            UPDATE,
            {
                "sensors": [
                    wind(
                        [{"name": "speed", "value": "fast", "unit": "m/s"}],
                        name="roof",
                    )
                ]
            },
            format="json",
        )

    as_committed()
    assert sensors_named("roof").count() == 2


def test_the_documented_property_example_fails_and_every_retry_adds_a_sensor(
    as_admin, space
):
    """DEFECT, pinned: the docs write ``speed`` where the code reads ``value``.

    The "Sensors *with* properties" example gives each property a key named
    after itself (``"speed": 5.0``). The code requires ``"value"``, so the
    request is a ``KeyError``. By then the sensor has been saved, so the first
    attempt leaves a sensor with no properties; the retry finds it, fails on
    the same property, and the exception handler creates another sensor with
    the same name. A device that keeps retrying a failing request grows the
    table by one sensor per attempt.
    """
    payload = {
        "sensors": [wind([{"name": "gust", "unit": "m/s", "speed": 5.0}])],
    }

    counts = []
    for _attempt in range(3):
        with pytest.raises(KeyError):
            as_admin().post(UPDATE, payload, format="json")
        counts.append(sensors_named("wind_sensor_01").count())

    assert counts == [1, 2, 3]


def test_sensors_that_are_not_a_list_of_objects_is_a_500(as_admin, space):
    """DEFECT, pinned: no shape check; a dict is iterated as its keys."""
    with pytest.raises(TypeError):
        as_admin().post(UPDATE, {"sensors": {"name": "test_sensor"}}, format="json")


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
