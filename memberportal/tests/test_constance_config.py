"""Characterisation: the Constance runtime settings contract.

Why this exists
---------------
Constance holds 109 runtime settings in the database: every feature flag, every
integration credential, all branding. It is read as ``config.X`` from models,
views and WebSocket consumers, so a bad upgrade does not fail loudly — it
silently serves wrong values, and the first symptom is a disabled feature or a
broken payment flow in production.

The project is on django-constance 2.9 and the modern release is 4.x, which is
a two-major-version jump that changes both the storage backend and the config
declaration format. These tests pin the current contract so that jump is
verifiable rather than hopeful.

The golden file is regenerated deliberately, never automatically. If a test
here fails, either the change was intended (update the golden file in the same
commit, and say why) or the upgrade broke something.
"""

import json
from pathlib import Path

import pytest

from membermatters.constance_config import (
    CONSTANCE_CONFIG,
    CONSTANCE_CONFIG_FIELDSETS,
)

pytestmark = pytest.mark.config

GOLDEN = json.loads(
    (Path(__file__).parent / "golden" / "constance_config.json").read_text()
)


def test_no_settings_are_lost_or_silently_added():
    """The exact key set is the contract. Additions are fine, but must be deliberate."""
    assert set(CONSTANCE_CONFIG) == set(GOLDEN["keys"]), (
        "The Constance key set changed. If this was intentional, regenerate "
        "tests/golden/constance_config.json in the same commit."
    )


def test_settings_keep_their_defaults_and_types():
    """A changed default silently reconfigures every fresh install."""
    mismatches = []
    for key, expected in GOLDEN["keys"].items():
        actual_default = CONSTANCE_CONFIG[key][0]
        actual_type = type(actual_default).__name__
        if actual_type != expected["type"] or actual_default != expected["default"]:
            mismatches.append(
                f"  {key}: expected {expected['type']}={expected['default']!r}, "
                f"got {actual_type}={actual_default!r}"
            )
    assert not mismatches, "Constance defaults drifted:\n" + "\n".join(mismatches)


def test_every_setting_has_help_text():
    """Constance renders help text in the admin; a missing one is a broken tuple."""
    missing = [
        key
        for key, spec in CONSTANCE_CONFIG.items()
        if len(spec) < 2 or not str(spec[1]).strip()
    ]
    assert not missing, f"Settings with no help text: {missing}"


def test_fieldsets_are_unchanged():
    assert {
        name: list(members) for name, members in CONSTANCE_CONFIG_FIELDSETS.items()
    } == GOLDEN["fieldsets"]


def test_every_setting_appears_in_exactly_one_fieldset():
    """A setting missing from the fieldsets is invisible in the admin UI."""
    seen = {}
    for fieldset, members in CONSTANCE_CONFIG_FIELDSETS.items():
        for key in members:
            seen.setdefault(key, []).append(fieldset)

    orphans = sorted(set(CONSTANCE_CONFIG) - set(seen))
    assert not orphans, f"Settings not shown in any fieldset: {orphans}"

    duplicated = {k: v for k, v in seen.items() if len(v) > 1}
    assert not duplicated, f"Settings listed in multiple fieldsets: {duplicated}"

    unknown = sorted(set(seen) - set(CONSTANCE_CONFIG))
    assert not unknown, f"Fieldsets reference settings that do not exist: {unknown}"


@pytest.mark.django_db
def test_every_setting_is_readable_through_the_database_backend():
    """Exercises the project's custom DatabaseBackend against every key.

    ``membermatters.constance_backend.DatabaseBackend`` overrides ``get()`` to
    stop django-constance swallowing database errors (which used to reset
    settings to defaults). That override is exactly the kind of thing a major
    upgrade breaks, so read every key through it.
    """
    from constance import config

    unreadable = []
    for key in CONSTANCE_CONFIG:
        try:
            getattr(config, key)
        except Exception as exc:  # noqa: BLE001 - reporting, not handling
            unreadable.append(f"  {key}: {type(exc).__name__}: {exc}")
    assert not unreadable, "Settings that could not be read:\n" + "\n".join(unreadable)


@pytest.mark.django_db
def test_unset_settings_fall_back_to_their_declared_default():
    from constance import config

    assert config.SITE_NAME == CONSTANCE_CONFIG["SITE_NAME"][0]
    assert config.ENABLE_DISCORD_INTEGRATION is False
    assert config.SMS_ENABLE is False


@pytest.mark.django_db
def test_settings_can_be_written_and_read_back():
    """The admin settings API writes Constance rows directly; prove round-trip."""
    from constance import config

    original = config.SITE_NAME
    try:
        config.SITE_NAME = "Round Trip Space"
        assert config.SITE_NAME == "Round Trip Space"
    finally:
        config.SITE_NAME = original


@pytest.mark.parametrize(
    "key",
    [
        "HOME_PAGE_CARDS",
        "WEBCAM_PAGE_URLS",
        "SMS_MESSAGES",
        "SPACE_DIRECTORY_PROJECTS",
        "STRIPE_MEMBERBUCKS_TOPUP_OPTIONS",
    ],
)
def test_json_valued_settings_have_parseable_defaults(key):
    """Several settings smuggle JSON through a CharField.

    Call sites are inconsistent about guarding the parse — some wrap it in
    try/except with a fallback, others let JSONDecodeError escape to a 500.
    At minimum the shipped defaults must parse.
    """
    json.loads(CONSTANCE_CONFIG[key][0])
