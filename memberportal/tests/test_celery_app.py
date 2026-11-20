"""Characterisation tests for ``membermatters/celeryapp.py``.

The task modules themselves are covered where their behaviour lives:
``api_metrics.tasks`` in ``test_metrics_api.py``, ``api_access.tasks`` in
``test_access_edges.py`` and ``api_general.tasks`` in
``test_api_general_gaps.py``. This file covers the app they hang off.
"""

import pytest


@pytest.fixture
def app():
    from membermatters.celeryapp import app

    app.loader.import_default_modules()
    return app


def test_the_app_is_named_for_the_portal(app):
    assert app.main == "membermatters"


def test_celery_settings_are_read_from_django_with_the_celery_prefix(app, settings):
    """``namespace="CELERY"``: ``CELERY_BROKER_URL`` becomes ``broker_url``."""
    assert app.conf.result_backend == settings.CELERY_RESULT_BACKEND == "django-db"
    assert app.conf.beat_scheduler == settings.CELERY_BEAT_SCHEDULER
    assert app.conf.broker_url == settings.CELERY_BROKER_URL
    assert settings.CELERY_BEAT_SCHEDULER.endswith("DatabaseScheduler")


def test_tasks_from_every_app_are_discovered(app):
    registered = set(app.tasks)

    assert "api_metrics.tasks.calculate_metrics" in registered
    assert "api_access.tasks.heartbeat_logger" in registered
    assert "membermatters.celeryapp.debug_task" in registered


def test_the_failure_logger_is_connected_to_celerys_signals(app):
    """``api_general.tasks`` only does anything if autodiscovery imported it."""
    from api_general import tasks
    from celery import signals

    for signal in (signals.task_failure, signals.task_revoked):
        assert any(
            getattr(receiver(), "__name__", None) == tasks.on_task_failure.__name__
            for _key, receiver, *_ in signal.receivers
        )


def test_the_debug_task_runs_and_logs_its_request(app, monkeypatch):
    from membermatters import celeryapp

    lines = []
    monkeypatch.setattr(celeryapp.logger, "debug", lines.append)

    result = celeryapp.debug_task.apply()

    assert result.successful()
    assert result.result is None
    assert len(lines) == 1 and lines[0].startswith("Request: ")
