"""Characterisation: the things that only fail at process start.

Written after the Django 4.2 upgrade booted cleanly under `manage.py check` and
the whole test suite, then died on `runserver` — because nothing had ever
imported ``membermatters.asgi``, which still carried a Django-3-only import.

Entry points deserve a test precisely because they sit outside the request path
that everything else exercises.
"""

import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "membermatters.asgi",
        "membermatters.wsgi",
        "membermatters.urls",
        "membermatters.websocket_urls",
        "membermatters.celeryapp",
        "membermatters.settings",
        "membermatters.oidc_provider_settings",
        "membermatters.middleware",
        "membermatters.custom_exception_handlers",
    ],
)
def test_entry_point_modules_import(module):
    importlib.import_module(module)


def test_asgi_application_routes_both_protocols():
    from membermatters.asgi import application

    assert set(application.application_mapping) == {"http", "websocket"}


def test_every_installed_app_and_its_admin_import():
    """Admin modules are imported by Django at startup but by little else."""
    from django.apps import apps

    for app_config in apps.get_app_configs():
        importlib.import_module(app_config.name)
        try:
            importlib.import_module(f"{app_config.name}.admin")
        except ModuleNotFoundError:
            pass  # not every app ships an admin module


def test_the_url_conf_resolves():
    """A broken view import anywhere surfaces as a failure to build the URL map."""
    from django.urls import get_resolver

    resolver = get_resolver()
    assert resolver.url_patterns
    # Spot-check that a route from each mounted app made it in.
    names = set(resolver.reverse_dict.keys())
    for expected in (
        "get_config",
        "UserAccessPermissions",
        "StripeWebhook",
        "GetMembers",
    ):
        assert expected in names, f"{expected} is missing from the URL conf"


@pytest.mark.django_db
def test_django_system_checks_pass():
    """Equivalent to `manage.py check`, run as part of the suite."""
    from django.core.management import call_command

    call_command("check")


@pytest.mark.django_db
def test_there_are_no_unapplied_model_changes():
    """Fails when a model was edited without generating the migration.

    Also catches a dependency upgrade that changes field definitions underneath
    us — exactly the kind of drift a Django major-version bump introduces.
    """
    from django.core.management import call_command

    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
