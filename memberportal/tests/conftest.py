"""Shared fixtures for the characterisation suite.

Two jobs here:

1. Make outbound network calls impossible-by-default, so a test can never
   depend on (or accidentally hit) Stripe, Postmark, Twilio, Discord, Slack,
   Trello, Vikunja, Canvas or Moodle.
2. Provide the small object graph the device protocol needs: an authorised
   device, a valid device API key, and an active member holding an RFID tag.
"""

import uuid

import pytest
import requests


# --------------------------------------------------------------------------
# Network containment
# --------------------------------------------------------------------------


class UnstubbedNetworkCall(AssertionError):
    """Raised when code under test tries to reach the outside world."""


@pytest.fixture(autouse=True)
def block_outbound_http(monkeypatch):
    """Fail loudly on any un-stubbed outbound HTTP.

    Most integrations are inert under Constance defaults (Discord, Slack and
    SMS are all disabled), but two are NOT and would otherwise reach the
    network from a test run:

    * ``POSTMARK_API_KEY`` defaults to the truthy string "PLEASE_CHANGE_ME",
      so ``services.emails.send_single_email`` takes the real send path.
    * ``ENABLE_STRIPE`` defaults to True.

    Patching ``requests.Session.request`` covers essentially every client in
    the tree, because stripe, twilio and postmarker all sit on top of requests.
    """

    def _boom(*args, **kwargs):
        target = args[2] if len(args) > 2 else kwargs.get("url", "<unknown>")
        raise UnstubbedNetworkCall(
            f"Un-stubbed outbound HTTP request to {target!r}. "
            f"Stub the service explicitly in the test."
        )

    monkeypatch.setattr(requests.Session, "request", _boom)
    for verb in ("request", "get", "post", "put", "patch", "delete", "head"):
        monkeypatch.setattr(requests, verb, _boom)


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture outbound email instead of sending it.

    Returns a list that accumulates one dict per send. ``services.emails``
    still renders the real template, so template breakage is caught.
    """
    captured = []

    class _FakeEmails:
        @staticmethod
        def send(**kwargs):
            captured.append(kwargs)
            return {"MessageID": str(uuid.uuid4())}

    class _FakePostmarkClient:
        def __init__(self, *args, **kwargs):
            self.emails = _FakeEmails()

    monkeypatch.setattr("services.emails.PostmarkClient", _FakePostmarkClient)
    return captured


# --------------------------------------------------------------------------
# Domain fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def make_member(db):
    """Create a User + Profile pair.

    Profile is the object almost every code path actually wants, but the two
    must be created together — ``User.__str__`` and much of the codebase
    assume ``user.profile`` exists.
    """
    from profile.models import Profile, User

    counter = {"n": 0}

    def _make(state="active", rfid=None, **profile_kwargs):
        counter["n"] += 1
        n = counter["n"]
        user = User.objects.create(email=f"member{n}@example.com", email_verified=True)
        user.set_password("test-password-not-a-secret")
        user.save()
        profile = Profile.objects.create(
            user=user,
            first_name="Test",
            last_name=f"Member{n}",
            screen_name=f"member{n}",
            state=state,
            rfid=rfid,
            **profile_kwargs,
        )
        return profile

    return _make


@pytest.fixture
def device_api_key(db):
    """A valid raw API key string that devices authenticate with.

    Note this key is global, not per-device: any device can authenticate with
    any valid key. That is the behaviour being characterised, not endorsed.
    """
    from access.models import AccessControlledDeviceAPIKey

    _obj, raw_key = AccessControlledDeviceAPIKey.objects.create_key(name="test-device")
    return raw_key


@pytest.fixture
def make_door(db):
    from access.models import Doors

    def _make(serial="door-serial-001", authorised=True, **kwargs):
        return Doors.objects.create(
            name=kwargs.pop("name", f"Test Door {serial}"),
            description="Door under test",
            serial_number=serial,
            authorised=authorised,
            **kwargs,
        )

    return _make


@pytest.fixture
def make_interlock(db):
    from access.models import Interlock

    def _make(serial="interlock-serial-001", authorised=True, **kwargs):
        return Interlock.objects.create(
            name=kwargs.pop("name", f"Test Interlock {serial}"),
            description="Interlock under test",
            serial_number=serial,
            authorised=authorised,
            **kwargs,
        )

    return _make


@pytest.fixture
def make_memberbucks_device(db):
    from access.models import MemberbucksDevice

    def _make(serial="memberbucks-serial-001", authorised=True, **kwargs):
        return MemberbucksDevice.objects.create(
            name=kwargs.pop("name", f"Test Vending {serial}"),
            description="Memberbucks device under test",
            serial_number=serial,
            authorised=authorised,
            **kwargs,
        )

    return _make


@pytest.fixture
def set_config(db):
    """Temporarily override Constance settings, restoring them afterwards.

    Consumers read ``config.X`` from a worker thread, so changes must go
    through the real backend rather than a patched attribute.
    """
    from constance import config

    originals = {}

    def _set(**kwargs):
        for key, value in kwargs.items():
            if key not in originals:
                originals[key] = getattr(config, key)
            setattr(config, key, value)

    yield _set

    for key, value in originals.items():
        setattr(config, key, value)


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def as_member(api_client):
    """Authenticate the DRF test client as the given profile's user."""

    def _login(profile):
        api_client.force_authenticate(user=profile.user)
        return api_client

    return _login


# --------------------------------------------------------------------------
# Stripe
# --------------------------------------------------------------------------


class FakeStripeObject(dict):
    """Mimics stripe's dual attribute/item access (``s.status`` and ``s["status"]``)."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


class StripeStub:
    """Records calls into the Stripe SDK and returns canned responses.

    Tests set a response with ``stripe_stub.set("Subscription.create", obj)``.
    Setting an ``Exception`` instance makes the call raise it, which is how the
    error branches in ``api_billing`` are exercised.
    """

    PATCHED = [
        "Customer.retrieve",
        "Customer.create",
        "Customer.modify",
        "SetupIntent.create",
        "PaymentMethod.retrieve",
        "PaymentMethod.attach",
        "PaymentMethod.detach",
        "Subscription.create",
        "Subscription.retrieve",
        "Subscription.modify",
        "PaymentIntent.create",
        "PaymentIntent.retrieve",
        "Product.create",
        "Price.create",
        "Webhook.construct_event",
    ]

    def __init__(self):
        self.calls = []
        self.responses = {}

    def set(self, path, value):
        self.responses[path] = value

    def calls_to(self, path):
        return [c for c in self.calls if c[0] == path]

    def called(self, path):
        return bool(self.calls_to(path))

    def _handler(self, path):
        def _fn(*args, **kwargs):
            self.calls.append((path, args, kwargs))
            value = self.responses.get(path)
            if isinstance(value, Exception):
                raise value
            if callable(value):
                return value(*args, **kwargs)
            return FakeStripeObject() if value is None else value

        return _fn


@pytest.fixture
def stripe_stub(monkeypatch):
    import stripe

    stub = StripeStub()
    for path in StripeStub.PATCHED:
        resource, method = path.split(".")
        monkeypatch.setattr(
            getattr(stripe, resource), method, stub._handler(path), raising=False
        )
    return stub


@pytest.fixture
def make_tier_and_plan(db):
    """A MemberTier (Stripe Product) with one PaymentPlan (Stripe Price)."""
    from api_admin_tools.models import MemberTier, PaymentPlan

    def _make(cost=2500, interval="month", interval_count=1, **plan_kwargs):
        tier = MemberTier.objects.create(
            name=plan_kwargs.pop("tier_name", "Full Member"),
            description=plan_kwargs.pop("tier_description", "Full membership"),
            stripe_id=plan_kwargs.pop("tier_stripe_id", "prod_test_full"),
            visible=True,
        )
        plan = PaymentPlan.objects.create(
            name="Monthly",
            stripe_id=plan_kwargs.pop("plan_stripe_id", "price_test_monthly"),
            member_tier=tier,
            visible=True,
            currency="aud",
            cost=cost,
            interval_count=interval_count,
            interval=interval,
            **plan_kwargs,
        )
        return tier, plan

    return _make


@pytest.fixture
def as_admin(api_client, make_member):
    """Authenticate as a staff user.

    ``permissions.IsAdminUser`` checks ``request.user.is_staff``, which on this
    project's custom User model is a property returning the ``staff`` boolean.
    """

    created = {}

    def _login(profile=None):
        if profile is None:
            # Reuse the same admin across repeated calls in one test; creating a
            # second one would collide on the unique rfid.
            profile = created.get("admin")
            if profile is None:
                profile = make_member(state="active", rfid="ADMIN-TAG")
                created["admin"] = profile
        profile.user.staff = True
        profile.user.save()
        api_client.force_authenticate(user=profile.user)
        api_client.admin_profile = profile
        return api_client

    return _login


@pytest.fixture
def with_api_key(api_client):
    """Authenticate with a generic DRF API key instead of a session.

    Several admin endpoints accept ``IsAdminUser | HasAPIKey``, which is how
    external tooling reads member and billing data.
    """
    from rest_framework_api_key.models import APIKey

    def _auth():
        _obj, raw_key = APIKey.objects.create_key(name="test-integration")
        api_client.credentials(HTTP_AUTHORIZATION=f"Api-Key {raw_key}")
        return api_client

    return _auth
