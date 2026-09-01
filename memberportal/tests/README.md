# Characterisation test suite

These tests describe how the system behaves today. They were written against
Django 3.2 as the acceptance criteria for the 4.2 → 5.2 upgrade — green before,
green after — and now serve the same role for the refactoring that follows.

That framing matters when reading them. Where a test pins behaviour that is
plainly wrong it is labelled `DEFECT, pinned`, and is paired with a
`@pytest.mark.xfail(strict=True)` test describing what the behaviour *should*
be. When someone fixes the defect the xfail flips to XPASS and the suite fails
loudly, forcing both tests to be updated together.

Every defect found this way has since been fixed, and each of those tests now
asserts the corrected behaviour while its docstring records what it used to do
and why. Two things are still pinned as wrong, because fixing either needs a
decision rather than a patch:

- `/api/billing/access-card/` accepts any unused tag with no proof the member
  holds that card. A duplicate is now rejected with a 400, but proving
  ownership needs a verification flow — swipe the card at a reader — not a
  uniqueness check.
- `AccessControlledDevice.all_members` is a column on every device type, but
  vending machines have no per-member access list for it to gate. The admin
  API no longer exposes it for that type; the column remains.
- `Kiosks` guards read and delete with `not authenticated and not staff`, which
  blocks only anonymous callers, so any logged-in member can list every kiosk
  id or delete one. See `test_auth_and_session.py`.
- Several smaller ones pinned in `test_auth_and_session.py`: registration reads
  `email` before checking it (a 500, not a 400); the password-reset endpoint
  reveals whether an address is registered, and 500s on an unknown token in the
  branch that changes the password; kiosk login ignores member state; repeated
  failed logins by an unverified member mint unbounded verification tokens.

## Running

```bash
cd memberportal
pip install -r requirements.txt -r requirements-dev.txt   # first time only
python -m pytest                                          # whole suite (~35s)
python -m pytest -m protocol                              # device WebSocket only
python -m pytest -m config                                # Constance settings only
python -m pytest --cov=. --cov-report=term-missing        # with coverage
```

`pytest.ini` sets the `MM_*` environment variables, because
`membermatters.settings` otherwise defaults every path to a Docker location and
opens the log file at import time.

## What is covered, and why

| Area | De-risks |
|---|---|
| `test_constance_config.py` | django-constance 2.9 → 4.x. 109 runtime settings live in the database; a bad migration silently serves wrong values rather than failing. |
| `test_device_connection.py` | channels 3 → 4. Connection, authentication and the commands shared by all three device types. |
| `test_device_protocol_door.py` | The tag sync contract. `get_tags()` decides who can physically enter the building. |
| `test_device_protocol_interlock.py` | Session lifecycle and costing — the only device type that moves money on its own. |
| `test_device_protocol_memberbucks.py` | Vending balance, debit/credit, rate limiting. |
| `test_signup_pipeline.py` | `can_signup()`, access-card assignment, Canvas/Moodle induction checks, complete/skip signup, registration and email verification. |
| `test_stripe_webhook.py` | The asynchronous route into membership: `invoice.paid`, `invoice.payment_failed`, `customer.subscription.deleted`, customer resolution and signature verification. |
| `test_billing_subscriptions.py` | Card storage, tier listing, plan signup, cancel/resume. |
| `test_boot.py` | Entry-point modules, the URL conf, system checks and migration drift — the failures that only appear at process start. |
| `test_admin_devices.py` | The three near-identical device CRUD classes, their per-type statistics, and the default-access side effects. |
| `test_admin_members.py` | Member list, activation, promotion, profile editing, access review, logs and billing info. |
| `test_admin_tiers_and_settings.py` | Tier and plan CRUD against Stripe, plus the Constance settings API. |
| `test_access_admin_endpoints.py` | Grant/revoke, the remote device commands, and the externally callable API-key subset. |
| `test_device_model_hierarchy.py` | What genuinely differs between the three device types — the per-type behaviour that used to live in the base class as a switch on `self.type`. |
| `test_auth_and_session.py` | Login (password, kiosk RFID, Discourse SSO), registration, email verification, password reset, the member-facing profile, and the site sessions that `get_tags()` consults. |

## Conventions

- **`transaction=True` is required** on anything touching a consumer. Sync
  consumers run in a worker thread with their own database connection, so the
  default transaction-wrapped `django_db` hides test data from them.
- **Outbound HTTP is blocked by default.** The autouse `block_outbound_http`
  fixture raises `UnstubbedNetworkCall` on any real request. Two integrations
  are live under Constance defaults and would otherwise hit the network:
  Postmark (`POSTMARK_API_KEY` defaults to the truthy `"PLEASE_CHANGE_ME"`) and
  Stripe (`ENABLE_STRIPE` defaults to `True`). Use the `sent_emails` fixture to
  capture mail.
- **Resolve foreign keys in sync context.** `profile.user.email` inside an
  `async def` test issues a lazy query and raises `SynchronousOnlyOperation`.
  Wrap it: `await ws.aget(lambda: profile.user.email)()`.
- **`tests/ws.py`** holds the WebSocket helpers — `open_authenticated()`
  performs the handshake and drains the server's initial state burst.
- **Stripe is stubbed, never called.** The `stripe_stub` fixture patches the SDK
  surface the billing views touch and records every call. Set a response with
  `stripe_stub.set("Subscription.create", obj)`; setting an `Exception` instance
  makes the call raise, which is how the error branches are exercised.
- **HTTP tests use `api_client` / `as_member(profile)`**, which force-authenticate
  DRF's `APIClient` rather than going through the login endpoint.

## Golden files

`golden/constance_config.json` is a deliberate snapshot, regenerated by hand,
never automatically. If a test fails against it, either the change was intended
— update the golden file in the same commit and say why in the message — or the
upgrade broke something.

## Response shapes are asserted exactly

The admin tests compare the full set of keys in each response, not a handful of
spot-checks. That is deliberate: the planned refactor replaces hand-built
response dicts with serializers, and the failure mode of that change is a
quietly dropped or renamed field rather than an exception. `set(body[0]) ==
DOOR_FIELDS` catches it; `assert body[0]["name"] == ...` does not.

The same reasoning drives
`test_the_three_device_shapes_have_drifted`, which pins the *differences*
between the door, interlock and vending payloads. They have already diverged,
and any consolidation must be a deliberate API decision rather than a
side effect.

## Not yet covered

The next batch worth writing, in rough priority order:

1. `UserResource` import/export round-trip. The 3 → 4 upgrade already caught
   one regression here by hand (the dropped `state` column); a round-trip test
   would have caught it automatically.
2. Session and JWT auth flows end to end, including the Discourse SSO handshake.
3. The member-facing tools endpoints (`/api/tools/*`) and meetings/proxies.
4. Anything on the frontend, which has no test runner at all.
