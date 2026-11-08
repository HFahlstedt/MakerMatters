"""Characterisation: the metrics pipeline in ``api_metrics``.

Four pieces, wired in a line:

1. ``api_metrics.metrics.calculate_*`` count members, subscriptions and
   memberbucks and store each result as a ``Metric`` row (one JSON blob per
   run, never updated).
2. ``api_metrics.tasks.calculate_metrics`` runs all six on a Celery beat
   schedule, then POSTs to ``/api/update-prom-metrics/``.
3. ``UpdatePromMetrics`` copies the newest row of each kind into Prometheus
   gauges. The gauges live in the web process, which is why the worker has to
   make an HTTP call to reach them.
4. ``Statistics`` serves the stored rows to the portal's stats page, one per
   day.

The last two links do not work today: ``UpdatePromMetrics`` raises on every
request once any ``Metric`` row exists, and the task that calls it never looks
at the answer. Tests that need to look past that failure patch the broken
lookup and say so.
"""

from datetime import timedelta

import pytest
import requests
from django.utils import timezone
from prometheus_client import REGISTRY

pytestmark = pytest.mark.django_db


ALL_METRICS = {
    "member_count_total",
    "member_count_6_months_total",
    "member_count_12_months_total",
    "subscription_count_total",
    "memberbucks_balance_total",
    "memberbucks_transactions_total",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def latest(name):
    from api_metrics.models import Metric

    return Metric.objects.filter(name=name).order_by("-creation_date", "-id").first()


def make_metric(name, data, when=None):
    from api_metrics.models import Metric

    return Metric.objects.create(
        name=name, data=data, creation_date=when or timezone.now()
    )


def backdate(profile, **delta):
    """Move a profile's ``created`` into the past; ``save()`` always resets it."""
    from profile.models import Profile

    Profile.objects.filter(pk=profile.pk).update(
        created=timezone.now() - timedelta(**delta)
    )


def set_balance(profile, balance):
    from profile.models import Profile

    Profile.objects.filter(pk=profile.pk).update(memberbucks_balance=balance)


def by_state(data):
    return {row["state"]: row["total"] for row in data}


def gauge(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


@pytest.fixture(autouse=True)
def fresh_gauges():
    """Gauges are process-global; labelled children outlive the test that set them."""
    import api_metrics.metrics as m

    for g in (
        m.member_count_total,
        m.member_count_6_months_total,
        m.member_count_12_months_total,
        m.subscription_count_total,
        m.memberbucks_transactions_total,
    ):
        g.clear()
    m.memberbucks_balance_total.set(0)


# --------------------------------------------------------------------------
# calculate_member_count and its 6 / 12 month variants
# --------------------------------------------------------------------------


def test_member_count_groups_profiles_by_state_smallest_group_first(make_member):
    import api_metrics.metrics as m

    make_member(state="active", rfid="MC-1")
    make_member(state="active", rfid="MC-2")
    make_member(state="active", rfid="MC-3")
    make_member(state="inactive", rfid="MC-4")
    make_member(state="noob", rfid="MC-5")
    make_member(state="noob", rfid="MC-6")

    m.calculate_member_count()

    assert latest("member_count_total").data == [
        {"state": "inactive", "total": 1},
        {"state": "noob", "total": 2},
        {"state": "active", "total": 3},
    ]


def test_member_count_with_no_members_stores_a_zero_row_not_an_empty_list():
    import api_metrics.metrics as m

    m.calculate_member_count()

    assert latest("member_count_total").data == [{"state": "active", "total": 0}]


def test_every_run_appends_a_row_and_never_updates_one(make_member):
    import api_metrics.metrics as m
    from api_metrics.models import Metric

    make_member(state="active", rfid="APPEND-1")

    m.calculate_member_count()
    m.calculate_member_count()

    assert Metric.objects.filter(name="member_count_total").count() == 2


def test_the_6_and_12_month_counts_only_include_members_older_than_that(make_member):
    """The gauge help text says ">6 months old", and that is the filter."""
    import api_metrics.metrics as m

    new = make_member(state="active", rfid="AGE-NEW")
    five = make_member(state="active", rfid="AGE-5")
    seven = make_member(state="inactive", rfid="AGE-7")
    thirteen = make_member(state="inactive", rfid="AGE-13")
    backdate(five, days=5 * 30)
    backdate(seven, days=7 * 31)
    backdate(thirteen, days=13 * 31)

    m.calculate_member_count_6_months()
    m.calculate_member_count_12_months()

    assert by_state(latest("member_count_6_months_total").data) == {"inactive": 2}
    assert by_state(latest("member_count_12_months_total").data) == {"inactive": 1}
    assert new.state == "active"  # the newest member is in neither


def test_the_6_and_12_month_counts_fall_back_to_the_same_zero_row():
    import api_metrics.metrics as m

    m.calculate_member_count_6_months()
    m.calculate_member_count_12_months()

    zero = [{"state": "active", "total": 0}]
    assert latest("member_count_6_months_total").data == zero
    assert latest("member_count_12_months_total").data == zero


# --------------------------------------------------------------------------
# calculate_subscription_count
# --------------------------------------------------------------------------


def test_subscription_count_groups_profiles_by_subscription_status(make_member):
    import api_metrics.metrics as m

    make_member(state="active", rfid="SUB-1", subscription_status="active")
    make_member(state="active", rfid="SUB-2", subscription_status="active")
    make_member(state="inactive", rfid="SUB-3")  # defaults to "inactive"

    m.calculate_subscription_count()

    assert latest("subscription_count_total").data == [
        {"state": "inactive", "total": 1},
        {"state": "active", "total": 2},
    ]


def test_subscription_count_with_no_members_stores_an_inactive_zero_row():
    """Not the "active" zero row the member counts use."""
    import api_metrics.metrics as m

    m.calculate_subscription_count()

    assert latest("subscription_count_total").data == [
        {"state": "inactive", "total": 0}
    ]


# --------------------------------------------------------------------------
# calculate_memberbucks_balance
# --------------------------------------------------------------------------


def test_the_memberbucks_balance_is_the_sum_of_profile_balances(make_member):
    import api_metrics.metrics as m

    set_balance(make_member(state="active", rfid="BAL-1"), 10.0)
    set_balance(make_member(state="active", rfid="BAL-2"), -5.0)
    set_balance(make_member(state="active", rfid="BAL-3"), 999.0)

    m.calculate_memberbucks_balance()

    assert latest("memberbucks_balance_total").data == {"value": 1004.0}


def test_a_balance_of_1000_or_more_is_left_out_of_the_total_without_a_word(
    make_member,
):
    """Pinned as behaviour: an undocumented outlier filter.

    ``filter(memberbucks_balance__lt=1000)`` drops any wallet holding $1000 or
    more from the total. Nothing says why, or in what unit -- balances are
    dollars -- and the stored row gives no hint that anything was excluded.
    Whether it guards against a runaway balance or hides a real one is a
    question for whoever set it up.
    """
    import api_metrics.metrics as m

    set_balance(make_member(state="active", rfid="BIG-1"), 20.0)
    set_balance(make_member(state="active", rfid="BIG-2"), 1000.0)
    set_balance(make_member(state="active", rfid="BIG-3"), 5000.0)

    m.calculate_memberbucks_balance()

    assert latest("memberbucks_balance_total").data == {"value": 20.0}


def test_the_memberbucks_balance_is_null_when_there_is_nothing_to_sum():
    """DEFECT, pinned: the other five metrics fall back to a zero row.

    ``aggregate(Sum(...))`` over no rows is ``None``, and it is stored as
    ``{"value": null}``. A site with no members, or none under the $1000
    cutoff, gets a null where the stats page and the Prometheus gauge both
    expect a number.
    """
    import api_metrics.metrics as m

    m.calculate_memberbucks_balance()

    assert latest("memberbucks_balance_total").data == {"value": None}


# --------------------------------------------------------------------------
# calculate_memberbucks_transactions
# --------------------------------------------------------------------------


def add_transaction(profile, amount, kind):
    from memberbucks.models import MemberBucks

    return MemberBucks.objects.create(
        user=profile.user, amount=amount, transaction_type=kind, description="test"
    )


def test_transactions_are_netted_per_type_largest_total_first(make_member):
    import api_metrics.metrics as m

    member = make_member(state="active", rfid="TX-1")
    add_transaction(member, 20.0, "stripe")
    add_transaction(member, 5.0, "stripe")
    add_transaction(member, -3.0, "card")
    add_transaction(member, -2.0, "card")

    m.calculate_memberbucks_transactions()

    assert latest("memberbucks_transactions_total").data == [
        {"type": "stripe", "total": 25.0},
        {"type": "card", "total": -5.0},
    ]


def test_a_single_transaction_of_1000_or_more_is_left_out_of_its_total(make_member):
    """Pinned as behaviour: the same silent $1000 cutoff, per row this time.

    The filter is on each ``MemberBucks`` row, not on the sum, so a $1500
    Stripe top-up vanishes from the stripe total while the $20 beside it is
    kept -- and the wallet it landed in is then over the balance cutoff too.
    """
    import api_metrics.metrics as m

    member = make_member(state="active", rfid="TX-BIG")
    add_transaction(member, 20.0, "stripe")
    add_transaction(member, 1500.0, "stripe")

    m.calculate_memberbucks_transactions()
    m.calculate_memberbucks_balance()

    assert latest("memberbucks_transactions_total").data == [
        {"type": "stripe", "total": 20.0}
    ]
    assert latest("memberbucks_balance_total").data == {"value": None}


def test_transactions_with_no_rows_store_a_stripe_zero_row():
    import api_metrics.metrics as m

    m.calculate_memberbucks_transactions()

    assert latest("memberbucks_transactions_total").data == [
        {"type": "stripe", "total": 0.0}
    ]


# --------------------------------------------------------------------------
# The Prometheus gauges
# --------------------------------------------------------------------------


def test_the_gauge_names_are_a_contract_with_whatever_scrapes_them():
    """Pinned as behaviour: two of the six lack the ``mm_`` prefix the rest carry.

    Dashboards and alerts key on these strings, so renaming them is a breaking
    change that no test in this repository would otherwise notice.
    """
    import api_metrics.metrics as m

    assert {
        g._name
        for g in (
            m.member_count_total,
            m.member_count_6_months_total,
            m.member_count_12_months_total,
            m.subscription_count_total,
            m.memberbucks_balance_total,
            m.memberbucks_transactions_total,
        )
    } == {
        "mm_member_count_total",
        "member_count_6_months_total",
        "member_count_12_months_total",
        "mm_subscription_count_total",
        "mm_memberbucks_balance_total",
        "mm_memberbucks_transactions_total",
    }


# --------------------------------------------------------------------------
# GET /api/statistics/
# --------------------------------------------------------------------------


def test_the_statistics_page_needs_a_logged_in_member(api_client):
    response = api_client.get("/api/statistics/")

    assert response.status_code in (401, 403)


def test_the_statistics_payload_is_on_site_plus_one_list_per_metric(
    as_member, make_member
):
    member = make_member(state="active", rfid="STAT-SHAPE")

    response = as_member(member).get("/api/statistics/")

    assert response.status_code == 200
    assert set(response.data) == {"on_site"} | ALL_METRICS
    assert all(response.data[name] == [] for name in ALL_METRICS)


def test_on_site_lists_whoever_is_signed_in_newest_first(as_member, make_member):
    from api_general.models import SiteSession

    viewer = make_member(state="active", rfid="SITE-V")
    early = make_member(state="active", rfid="SITE-E")
    late = make_member(state="active", rfid="SITE-L")
    gone = make_member(state="active", rfid="SITE-G")
    now = timezone.now()
    SiteSession.objects.create(user=early.user, signin_date=now - timedelta(hours=3))
    SiteSession.objects.create(user=late.user, signin_date=now - timedelta(hours=1))
    SiteSession.objects.create(
        user=gone.user,
        signin_date=now - timedelta(hours=5),
        signout_date=now - timedelta(hours=4),
    )

    response = as_member(viewer).get("/api/statistics/")

    assert response.data["on_site"] == {
        "count": 2,
        "members": [late.get_full_name(), early.get_full_name()],
    }


def test_the_stats_series_returns_the_last_row_of_each_day_oldest_first(
    as_member, make_member
):
    member = make_member(state="active", rfid="STAT-DAY")
    day = (timezone.now() - timedelta(days=10)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    make_metric("member_count_total", "first", day + timedelta(hours=1))
    make_metric("member_count_total", "second", day + timedelta(hours=5))
    last_of_day = make_metric("member_count_total", "third", day + timedelta(hours=9))
    next_day = make_metric("member_count_total", "next", day + timedelta(hours=30))
    make_metric("subscription_count_total", "other", day + timedelta(hours=2))

    response = as_member(member).get("/api/statistics/")

    assert response.data["member_count_total"] == [
        {"date": last_of_day.creation_date, "data": "third"},
        {"date": next_day.creation_date, "data": "next"},
    ]
    assert [r["data"] for r in response.data["subscription_count_total"]] == ["other"]


def test_a_stats_day_is_a_utc_day_not_a_local_one(as_member, make_member):
    """Pinned as behaviour: the day is cut at 00:00 UTC.

    The query groups on SQL ``date(creation_date)``, which is the stored UTC
    date, while the portal runs in ``TIME_ZONE`` (Australia/Brisbane, UTC+10
    by default). Two rows eight hours apart that fall on one local day are two
    days to this endpoint.
    """
    member = make_member(state="active", rfid="STAT-UTC")
    midnight = (timezone.now() - timedelta(days=10)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    make_metric("member_count_total", "before", midnight - timedelta(hours=1))
    make_metric("member_count_total", "after", midnight + timedelta(hours=1))

    response = as_member(member).get("/api/statistics/")

    assert [r["data"] for r in response.data["member_count_total"]] == [
        "before",
        "after",
    ]


def test_stats_max_days_bounds_how_far_back_the_series_goes(
    as_member, make_member, set_config
):
    member = make_member(state="active", rfid="STAT-WINDOW")
    set_config(STATS_MAX_DAYS=30)
    make_metric("member_count_total", "old", timezone.now() - timedelta(days=40))
    make_metric("member_count_total", "recent", timezone.now() - timedelta(days=10))

    response = as_member(member).get("/api/statistics/")

    assert [r["data"] for r in response.data["member_count_total"]] == ["recent"]


def test_with_the_stats_page_off_members_get_empty_series_but_still_see_on_site(
    as_member, make_member, set_config
):
    from api_general.models import SiteSession

    member = make_member(state="active", rfid="STAT-OFF")
    SiteSession.objects.create(user=member.user)
    make_metric("member_count_total", "hidden")
    set_config(ENABLE_STATS_PAGE=False)

    response = as_member(member).get("/api/statistics/")

    assert response.data["member_count_total"] == []
    assert response.data["on_site"]["count"] == 1


def test_with_the_stats_page_off_only_the_admin_flag_unlocks_the_data(
    as_member, as_admin, make_member, set_config
):
    """Pinned as behaviour: this view asks ``user.is_admin``, not ``is_staff``.

    The rest of the API gates on ``is_staff`` (``IsAdminUser``). The two are
    separate columns on the User, so a staff member without the ``admin`` flag
    -- including the staff user the admin tests log in as -- is treated like
    any other member here.
    """
    set_config(ENABLE_STATS_PAGE=False)
    make_metric("member_count_total", "visible")
    staff = as_admin()
    assert staff.admin_profile.user.is_staff
    assert not staff.admin_profile.user.is_admin

    assert staff.get("/api/statistics/").data["member_count_total"] == []

    staff.admin_profile.user.admin = True
    staff.admin_profile.user.save()
    assert [
        r["data"] for r in staff.get("/api/statistics/").data["member_count_total"]
    ] == ["visible"]


# --------------------------------------------------------------------------
# PUT /api/update-statistics/
# --------------------------------------------------------------------------


def test_update_statistics_is_closed_to_anonymous_callers_and_plain_members(
    api_client, as_member, make_member
):
    from api_metrics.models import Metric

    assert api_client.put("/api/update-statistics/").status_code in (401, 403)

    member = make_member(state="active", rfid="UPD-MEMBER")
    assert as_member(member).put("/api/update-statistics/").status_code == 403
    assert not Metric.objects.exists()


def test_staff_can_recalculate_every_metric_at_once(as_admin):
    from api_metrics.models import Metric

    response = as_admin().put("/api/update-statistics/")

    assert response.status_code == 200
    assert sorted(Metric.objects.values_list("name", flat=True)) == sorted(ALL_METRICS)


def test_an_integration_api_key_can_recalculate_too(with_api_key):
    from api_metrics.models import Metric

    response = with_api_key().put("/api/update-statistics/")

    assert response.status_code == 200
    assert Metric.objects.count() == 6


# --------------------------------------------------------------------------
# POST /api/update-prom-metrics/
# --------------------------------------------------------------------------


@pytest.fixture
def prom_lookup_working(monkeypatch):
    """Make ``getattr(api_metrics.metrics, name)`` find the gauges.

    In ``views.py`` the module is imported as ``api_metrics``, so
    ``api_metrics.metrics`` asks it for an attribute called ``metrics`` that
    it does not have. Pointing that attribute back at the module is what the
    line plainly meant, and lets the tests below describe everything past it.
    """
    import api_metrics.metrics as m

    monkeypatch.setattr(m, "metrics", m, raising=False)


def test_update_prom_metrics_needs_no_login_and_no_key(api_client):
    """Pinned as behaviour: ``AllowAny``, deliberately -- the Celery task that
    calls it sends no credentials. Anyone who can reach the site can make it
    read the database, so whether it should be open is a deployment decision.
    """
    assert api_client.post("/api/update-prom-metrics/").status_code == 200
    assert api_client.get("/api/update-prom-metrics/").status_code == 405


def test_update_prom_metrics_raises_as_soon_as_any_metric_exists():
    """DEFECT, pinned: the gauges can never be updated.

    ``views.py`` does ``import api_metrics.metrics as api_metrics`` and then
    ``getattr(api_metrics.metrics, metric.name)``. That is the module
    ``api_metrics.metrics`` looking for a ``metrics`` attribute inside itself,
    so every request that finds a stored metric is an AttributeError and a 500.
    With an empty table there is nothing to look up, which is why the test
    above passes and why this has not been noticed.
    """
    from rest_framework.test import APIClient

    make_metric("member_count_total", [{"state": "active", "total": 3}])

    with pytest.raises(AttributeError, match="has no attribute 'metrics'"):
        APIClient().post("/api/update-prom-metrics/")


def test_update_prom_metrics_copies_the_stored_rows_into_the_gauges(
    api_client, prom_lookup_working
):
    """Past the broken lookup (patched), each row lands on its own gauge."""
    make_metric("member_count_total", [{"state": "active", "total": 7}])
    make_metric("member_count_6_months_total", [{"state": "active", "total": 5}])
    make_metric("member_count_12_months_total", [{"state": "inactive", "total": 2}])
    make_metric("subscription_count_total", [{"state": "active", "total": 4}])
    make_metric("memberbucks_balance_total", {"value": 123.5})
    make_metric(
        "memberbucks_transactions_total",
        [{"type": "stripe", "total": 40.0}, {"type": "card", "total": -6.0}],
    )

    response = api_client.post("/api/update-prom-metrics/")

    assert response.status_code == 200
    assert gauge("mm_member_count_total", state="active") == 7
    assert gauge("member_count_6_months_total", state="active") == 5
    assert gauge("member_count_12_months_total", state="inactive") == 2
    assert gauge("mm_subscription_count_total", state="active") == 4
    assert gauge("mm_memberbucks_balance_total") == 123.5
    assert gauge("mm_memberbucks_transactions_total", type="stripe") == 40.0
    assert gauge("mm_memberbucks_transactions_total", type="card") == -6.0


def test_update_prom_metrics_uses_only_the_newest_row_of_each_kind(
    api_client, prom_lookup_working
):
    now = timezone.now()
    make_metric(
        "member_count_total",
        [{"state": "active", "total": 1}],
        now - timedelta(days=2),
    )
    make_metric("member_count_total", [{"state": "active", "total": 9}], now)

    api_client.post("/api/update-prom-metrics/")

    assert gauge("mm_member_count_total", state="active") == 9


def test_a_state_that_disappears_keeps_its_last_value_on_the_gauge(
    api_client, make_member, prom_lookup_working
):
    """DEFECT, pinned (behind the lookup defect above): gauges are never cleared.

    Each run does ``labels(state=...).set(total)`` for the states present and
    nothing for the states that are not. When the last "noob" is promoted, the
    next stored row has no "noob" entry, and the gauge goes on reporting the
    old count until the web process restarts.
    """
    import api_metrics.metrics as m

    member = make_member(state="noob", rfid="STALE-1")
    m.calculate_member_count()
    api_client.post("/api/update-prom-metrics/")
    assert gauge("mm_member_count_total", state="noob") == 1

    member.state = "active"
    member.save()
    m.calculate_member_count()
    latest_row = latest("member_count_total")
    assert by_state(latest_row.data) == {"active": 1}
    api_client.post("/api/update-prom-metrics/")

    assert gauge("mm_member_count_total", state="active") == 1
    assert gauge("mm_member_count_total", state="noob") == 1  # should be gone


def test_a_null_memberbucks_balance_makes_the_gauge_update_raise(
    api_client, prom_lookup_working
):
    """DEFECT, pinned (behind the lookup defect above): ``set(None)``.

    ``calculate_memberbucks_balance`` stores ``{"value": null}`` when there is
    nothing to sum, and the gauge is set from it unchecked. ``Gauge.set``
    calls ``float(None)``. The metrics that sort after the balance in the loop
    are never reached.
    """
    make_metric("memberbucks_balance_total", {"value": None})

    with pytest.raises(TypeError):
        api_client.post("/api/update-prom-metrics/")


# --------------------------------------------------------------------------
# Celery wiring (api_metrics.tasks)
# --------------------------------------------------------------------------


class RecordingLogger:
    def __init__(self):
        self.records = []

    def _log(self, level):
        return lambda message, *a, **k: self.records.append((level, message))

    def __getattr__(self, level):
        return self._log(level)

    def at(self, level):
        return [message for lvl, message in self.records if lvl == level]


class FakeSender:
    def __init__(self):
        self.scheduled = []

    def add_periodic_task(self, interval, signature, **kwargs):
        self.scheduled.append((interval, signature.task, kwargs))


@pytest.fixture
def task_log(monkeypatch):
    import api_metrics.tasks as tasks

    log = RecordingLogger()
    monkeypatch.setattr(tasks, "logger", log)
    return log


def schedule(set_config, interval):
    import api_metrics.tasks as tasks

    set_config(METRICS_INTERVAL=interval)
    sender = FakeSender()
    tasks.setup_periodic_tasks(sender)
    return sender.scheduled


def test_the_task_is_scheduled_at_the_configured_interval(set_config, task_log):
    import api_metrics.tasks as tasks

    scheduled = schedule(set_config, 86400)

    assert scheduled == [
        (
            86400,
            tasks.calculate_metrics.name,
            {"expires": 60, "name": "celery_calculate_metrics"},
        )
    ]
    assert task_log.at("warning") == []


def test_the_shipped_default_interval_warns_that_it_is_not_recommended(
    set_config, task_log
):
    """Pinned as behaviour: METRICS_INTERVAL defaults to 3600, and anything
    under 24 hours logs "NOT recommended for production" -- so a stock install
    warns about its own default on every worker start."""
    scheduled = schedule(set_config, 3600)

    assert [interval for interval, _, _ in scheduled] == [3600]
    assert len(task_log.at("warning")) == 1
    assert "NOT recommended" in task_log.at("warning")[0]


def test_an_interval_under_a_minute_is_raised_to_a_minute(set_config, task_log):
    scheduled = schedule(set_config, 5)

    assert [interval for interval, _, _ in scheduled] == [60]
    assert len(task_log.at("warning")) == 2


def test_an_interval_of_zero_schedules_nothing(set_config, task_log):
    assert schedule(set_config, 0) == []


@pytest.fixture
def post_to_site(monkeypatch):
    """Replace the task's ``requests.post``; returns the list of calls."""
    import api_metrics.tasks as tasks

    calls = []

    class Reply:
        status_code = 200

    def _post(*args, **kwargs):
        calls.append((args, kwargs))
        return Reply()

    monkeypatch.setattr(tasks.requests, "post", _post)
    return calls


def test_calculate_metrics_stores_all_six_then_pokes_the_web_process(
    set_config, post_to_site, task_log
):
    import api_metrics.tasks as tasks
    from api_metrics.models import Metric

    set_config(SITE_URL="https://portal.example.test")

    tasks.calculate_metrics()

    assert sorted(Metric.objects.values_list("name", flat=True)) == sorted(ALL_METRICS)
    # No credentials and no timeout: the call is a bare POST and waits as long
    # as the server takes.
    assert post_to_site == [
        (("https://portal.example.test/api/update-prom-metrics/",), {})
    ]


def test_a_trailing_slash_on_site_url_gives_a_double_slash(
    set_config, post_to_site, task_log
):
    """Pinned as behaviour: the URL is built by concatenation, not joined."""
    import api_metrics.tasks as tasks

    set_config(SITE_URL="https://portal.example.test/")

    tasks.calculate_metrics()

    assert post_to_site[0][0] == (
        "https://portal.example.test//api/update-prom-metrics/",
    )


def test_a_connection_failure_is_logged_and_the_rows_are_kept(
    set_config, monkeypatch, task_log
):
    import api_metrics.tasks as tasks
    from api_metrics.models import Metric

    def _refuse(*args, **kwargs):
        raise requests.ConnectionError("web process is down")

    monkeypatch.setattr(tasks.requests, "post", _refuse)

    tasks.calculate_metrics()  # does not raise

    assert Metric.objects.count() == 6
    assert len(task_log.at("error")) == 1
    assert "web process is down" in task_log.at("error")[0]


def test_an_error_response_from_the_web_process_is_not_noticed(
    monkeypatch, set_config, task_log
):
    """DEFECT, pinned: the reply is never looked at.

    Only an exception from ``requests.post`` is caught. A 500 from
    ``/api/update-prom-metrics/`` -- which is what it returns today -- is a
    normal return, so the task finishes without a word in the log and the
    gauges quietly never move.
    """
    import api_metrics.tasks as tasks

    class Broken:
        status_code = 500

        def raise_for_status(self):
            raise requests.HTTPError("500 Server Error")

    monkeypatch.setattr(tasks.requests, "post", lambda *a, **k: Broken())

    tasks.calculate_metrics()

    assert task_log.at("error") == []
