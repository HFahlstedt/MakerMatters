"""Characterisation tests for ``profile.models.UserManager``.

The manager is how users are created by ``manage.py createsuperuser`` and how
Django's ``authenticate()`` looks a user up at login.
"""

import pytest
from django.contrib.auth import authenticate
from django.core.management import call_command

pytestmark = pytest.mark.django_db


@pytest.fixture
def User():
    from profile.models import User

    return User


# --------------------------------------------------------------------------
# create_user
# --------------------------------------------------------------------------


def test_create_user_saves_a_user_with_a_hashed_password(User):
    user = User.objects.create_user("new@example.com", "s3cret-pass")

    assert user.pk is not None
    assert User.objects.get(pk=user.pk).email == "new@example.com"
    assert user.password != "s3cret-pass"
    assert user.check_password("s3cret-pass")


def test_create_user_makes_an_ordinary_user(User):
    user = User.objects.create_user("new@example.com", "pw")

    assert (user.staff, user.admin, user.is_superuser) == (False, False, False)
    assert user.is_active is True
    assert user.email_verified is True


def test_create_user_does_not_create_a_profile(User):
    """The profile is a separate row that the caller must create, and much of
    the portal assumes it exists (``User.__str__`` falls back to
    ``(NO PROFILE)``)."""
    from profile.models import Profile

    user = User.objects.create_user("new@example.com", "pw")

    assert not Profile.objects.filter(user=user).exists()
    assert str(user) == "(NO PROFILE) - new@example.com"


@pytest.mark.parametrize("email", ["", None])
def test_create_user_requires_an_email(User, email):
    with pytest.raises(ValueError, match="email address"):
        User.objects.create_user(email, "pw")

    assert User.objects.count() == 0


def test_create_user_lowercases_only_the_domain_of_the_address(User):
    user = User.objects.create_user("Some.One@EXAMPLE.COM", "pw")

    assert user.email == "Some.One@example.com"


def test_create_user_without_a_password_makes_the_account_unusable(User):
    user = User.objects.create_user("new@example.com")

    assert user.has_usable_password() is False
    assert authenticate(username="new@example.com", password="") is None


def test_create_user_can_make_a_superuser_without_the_other_flags(User):
    """``is_superuser`` is independent of ``staff`` and ``admin``, the two
    flags the portal itself checks."""
    user = User.objects.create_user("new@example.com", "pw", is_superuser=True)

    assert (user.staff, user.admin, user.is_superuser) == (False, False, True)


def test_a_duplicate_email_is_rejected(User):
    User.objects.create_user("new@example.com", "pw")

    with pytest.raises(ValueError, match="already exists"):
        User.objects.create_user("new@example.com", "pw2")


# --------------------------------------------------------------------------
# create_staffuser / create_superuser
# --------------------------------------------------------------------------


def test_create_staffuser_sets_only_staff(User):
    user = User.objects.create_staffuser("staff@example.com", "pw")

    assert (user.staff, user.admin, user.is_superuser) == (True, False, False)
    assert User.objects.get(pk=user.pk).staff is True
    assert user.check_password("pw")


def test_create_superuser_sets_all_three_flags(User):
    user = User.objects.create_superuser("root@example.com", "pw")

    assert (user.staff, user.admin, user.is_superuser) == (True, True, True)
    stored = User.objects.get(pk=user.pk)
    assert (stored.staff, stored.admin, stored.is_superuser) == (True, True, True)
    assert user.check_password("pw")


def test_createsuperuser_command_uses_the_manager(User, monkeypatch):
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "cmd-pass")

    call_command("createsuperuser", "--noinput", "--email", "cmd@example.com")

    user = User.objects.get(email="cmd@example.com")
    assert (user.staff, user.admin, user.is_superuser) == (True, True, True)
    assert user.check_password("cmd-pass")


# --------------------------------------------------------------------------
# get_by_natural_key (login lookup)
# --------------------------------------------------------------------------


def test_the_login_lookup_ignores_case(User):
    user = User.objects.create_user("Mixed.Case@example.com", "pw")

    assert User.objects.get_by_natural_key("mixed.case@EXAMPLE.com") == user
    assert authenticate(username="MIXED.CASE@example.com", password="pw") == user


def test_an_unknown_login_is_a_clean_authentication_failure(User):
    assert authenticate(username="nobody@example.com", password="pw") is None


def test_a_wrong_password_fails_authentication(User):
    User.objects.create_user("new@example.com", "pw")

    assert authenticate(username="new@example.com", password="wrong") is None


def test_is_active_is_not_a_stored_field_so_a_login_cannot_be_disabled(User):
    """Pinned as behaviour: ``is_active`` is Django's class-level ``True``, not
    a column, so setting it does not survive a reload. Whether a member may log
    in is decided by the portal (``Profile.state``), never by Django."""
    user = User.objects.create_user("new@example.com", "pw")
    user.is_active = False
    user.save()

    assert "is_active" not in [f.name for f in User._meta.get_fields()]
    assert authenticate(username="new@example.com", password="pw") is not None


def test_create_user_rejects_an_address_that_differs_only_in_case(User):
    """Used to be allowed: ``email`` is unique case-sensitively, so the pair
    could be created and then broke the case-insensitive login lookup."""
    User.objects.create_user("Ann@example.com", "pw")

    with pytest.raises(ValueError, match="already exists"):
        User.objects.create_user("ann@example.com", "pw2")

    assert User.objects.count() == 1


def test_a_case_variant_pair_made_outside_the_manager_can_each_sign_in(User):
    """Used to raise ``MultipleObjectsReturned`` for both. Rows made with
    ``User.objects.create`` (a shell, the Django admin, old data) bypass the
    manager's check, so the lookup prefers an exact match."""
    upper = User.objects.create(email="Ann@example.com")
    upper.set_password("pw-upper")
    upper.save()
    lower = User.objects.create(email="ann@example.com")
    lower.set_password("pw-lower")
    lower.save()

    assert authenticate(username="Ann@example.com", password="pw-upper") == upper
    assert authenticate(username="ann@example.com", password="pw-lower") == lower


def test_an_ambiguous_login_that_matches_neither_exactly_fails_cleanly(User):
    User.objects.create(email="Ann@example.com")
    User.objects.create(email="ann@example.com")

    assert authenticate(username="ANN@example.com", password="pw") is None
