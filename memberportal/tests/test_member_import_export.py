"""Characterisation: the member import/export offered by the Django admin.

``UserResource`` in ``profile/admin.py`` backs the "Import" and "Export"
buttons on the user list. It is the only bulk write path in the system, and
the only one where a mistake corrupts the member roster silently rather than
raising at an endpoint.

The awkwardness it works around is real: ``User`` holds only ``email``,
``staff`` and ``admin``; every human detail (name, screen name, RFID tag,
membership state) lives on the one-to-one ``Profile``. The resource bridges
that gap with a declared field per profile column.

Those fields used to carry a ``ForeignKeyWidget(Profile, "<column>")``
despite none of them being a foreign key, which on import made the widget do
its usual job -- ``Profile.objects.get(<column>=<cell>)`` -- instead of
storing the value. The tests below were written against that version, and
several of them record what it did; they now assert the behaviour that
replaced it. See ``test_a_freshly_exported_file_can_be_imported_back``, which
is the round-trip the feature exists for and which never worked.
"""

import pytest
import tablib

pytestmark = pytest.mark.django_db


#: The column order declared in ``UserResource.Meta.fields``.
COLUMNS = [
    "email",
    "staff",
    "admin",
    "first_name",
    "last_name",
    "screen_name",
    "rfid",
    "state",
]


def resource():
    from profile.admin import UserResource

    return UserResource()


def dataset(*rows, headers=None):
    data = tablib.Dataset(headers=list(headers or COLUMNS))
    for row in rows:
        data.append(list(row))
    return data


def member_row(email, **overrides):
    """One import row, defaulting to something plausible."""
    values = {
        "email": email,
        "staff": "0",
        "admin": "0",
        "first_name": "Alice",
        "last_name": "Anders",
        "screen_name": "alice",
        "rfid": "TAG-A",
        "state": "noob",
    }
    values.update(overrides)
    return [values[column] for column in COLUMNS]


def import_rows(*rows, headers=None):
    return resource().import_data(
        dataset(*rows, headers=headers), dry_run=False, raise_errors=False
    )


def row_results(result):
    """(import_type, [repr of each error]) for each row, in order."""
    return [
        (row.import_type, [repr(error.error) for error in row.errors])
        for row in result.rows
    ]


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_the_export_carries_exactly_the_declared_columns(make_member):
    """The whitelist is load-bearing and has already lost a column once.

    Fields declared as attributes on a Resource used to bypass
    ``Meta.fields``; django-import-export 4.0 made the whitelist apply to
    them too, so ``state`` silently vanished from the export until it was
    listed explicitly. Asserting the exact header set is what turns a repeat
    of that into a failing test rather than a quiet data loss.
    """
    make_member(state="active", rfid="EXPORT-1")

    assert resource().export().headers == COLUMNS


def test_the_export_reads_the_member_details_from_the_profile(make_member):
    make_member(state="active", rfid="EXPORT-2")

    (row,) = resource().export().dict

    assert row["email"] == "member1@example.com"
    assert row["first_name"] == "Test"
    assert row["last_name"] == "Member1"
    assert row["rfid"] == "EXPORT-2"
    assert row["state"] == "active"


def test_the_export_includes_the_screen_name(make_member):
    """This column used to come out blank in every export ever taken.

    The hook meant to fill it was named ``dehydrate_screen_name_name`` -- one
    ``_name`` too many. django-import-export dispatches on the exact name
    ``dehydrate_<field>``, so it never ran, and the field fell back to
    looking up ``screen_name`` on ``User``, where there is no such attribute.
    """
    member = make_member(state="active", rfid="EXPORT-3")

    (row,) = resource().export().dict

    assert row["screen_name"] == member.screen_name == "member1"


def test_a_user_with_no_profile_exports_blanks_instead_of_failing(db):
    """A profile-less ``User`` is reachable -- ``User.__str__`` guards against
    it too -- so it is not worth failing a whole download over."""
    from profile.models import User

    User.objects.create(email="ghost@example.com")

    (row,) = resource().export().dict

    assert row["email"] == "ghost@example.com"
    assert row["first_name"] == ""
    assert row["last_name"] == ""
    assert row["screen_name"] == ""
    assert row["rfid"] is None
    # Not "" -- the fallback for a missing profile is the model's own default.
    assert row["state"] == "noob"


# --------------------------------------------------------------------------
# Import: creating members
# --------------------------------------------------------------------------


def test_a_new_member_is_created_with_their_profile():
    from profile.models import Profile, User

    result = import_rows(member_row("new@example.com", state="active"))

    assert row_results(result) == [("new", [])]
    user = User.objects.get(email="new@example.com")
    assert user.email_verified is True
    profile = Profile.objects.get(user=user)
    assert (profile.first_name, profile.last_name) == ("Alice", "Anders")
    assert (profile.screen_name, profile.rfid) == ("alice", "TAG-A")
    assert profile.state == "active"


def test_a_created_member_is_reported_as_new():
    """The import summary used to say "updated" for every row, however many
    members the file added.

    ``before_import_row`` created the user itself, before
    django-import-export looked one up, so by the time it did the row always
    matched something that already existed. Nothing creates the user ahead of
    the library any more, so the count in the admin's preview is real.
    """
    result = import_rows(member_row("counted@example.com", state="active"))

    assert row_results(result) == [("new", [])]


def test_a_member_can_be_imported_in_any_state():
    """A row used to import only when its state was the model default.

    ``before_import_row`` created the profile without a state, so it started
    at "noob", and the ``state`` field then ran
    ``Profile.objects.get(state=<cell>)`` through its ForeignKeyWidget. Those
    two agreed only when the cell also said "noob"; every other state failed
    the row outright. That is the whole of why importing appeared to work.
    """
    from profile.models import Profile

    result = import_rows(
        member_row("s1@example.com", screen_name="s1", rfid="T1", state="noob"),
        member_row("s2@example.com", screen_name="s2", rfid="T2", state="active"),
        member_row("s3@example.com", screen_name="s3", rfid="T3", state="inactive"),
        member_row("s4@example.com", screen_name="s4", rfid="T4", state="accountonly"),
    )

    assert row_results(result) == [("new", [])] * 4
    assert sorted(Profile.objects.values_list("state", flat=True)) == [
        "accountonly",
        "active",
        "inactive",
        "noob",
    ]


def test_members_sharing_a_column_value_no_longer_collide():
    """Two members with the same first name used to break the second row.

    Every profile column was looked up with ``.get()``, and none of
    first name, last name, screen name or state is unique, so the second
    member to share one raised ``MultipleObjectsReturned``.
    """
    result = import_rows(
        member_row("one@example.com", first_name="Same", screen_name="s1", rfid="T1"),
        member_row("two@example.com", first_name="Same", screen_name="s2", rfid="T2"),
    )

    assert row_results(result) == [("new", [])] * 2


def test_a_freshly_exported_file_can_be_imported_back(make_member):
    """The round-trip this feature exists for, which never used to work.

    Export a member, drop them, import the exact file back. The ``state``
    cell said "active", the widget lookup failed, and the restore produced
    nothing at all.
    """
    from profile.models import Profile, User

    make_member(state="active", rfid="ROUND-1")
    exported = resource().export()

    User.objects.all().delete()
    result = resource().import_data(exported, dry_run=False, raise_errors=False)

    assert row_results(result) == [("new", [])]
    profile = Profile.objects.get(user__email="member1@example.com")
    assert (profile.first_name, profile.last_name) == ("Test", "Member1")
    assert (profile.screen_name, profile.rfid) == ("member1", "ROUND-1")
    assert profile.state == "active"


def test_a_blank_rfid_column_is_stored_as_null():
    """The column is unique, so two members holding "" would collide."""
    from profile.models import Profile

    result = import_rows(
        member_row("no-tag-1@example.com", screen_name="n1", rfid=""),
        member_row("no-tag-2@example.com", screen_name="n2", rfid=""),
    )

    assert row_results(result) == [("new", [])] * 2
    assert list(Profile.objects.values_list("rfid", flat=True)) == [None, None]


# --------------------------------------------------------------------------
# Import: updating members
# --------------------------------------------------------------------------


def test_an_existing_member_is_updated(make_member):
    """Import used to be able to create a member but never update one.

    ``before_import_row`` wrote the profile columns only on the ``created``
    branch of its ``get_or_create``, so for anyone who already existed the
    whole row was discarded.
    """
    member = make_member(state="noob", rfid="TAG-OLD")

    result = import_rows(
        member_row(
            member.user.email,
            staff="1",
            first_name="NewFirst",
            last_name="NewLast",
            screen_name="newscreen",
            rfid="TAG-NEW",
            state="active",
        )
    )

    assert row_results(result) == [("update", [])]
    member.refresh_from_db()
    member.user.refresh_from_db()
    assert (member.first_name, member.last_name) == ("NewFirst", "NewLast")
    assert (member.screen_name, member.rfid) == ("newscreen", "TAG-NEW")
    assert member.state == "active"
    assert member.user.staff is True


def test_a_partial_file_updates_only_the_columns_it_names(make_member):
    """A file missing a column used to fail every row with a bare ``KeyError``.

    ``before_import_row`` indexed ``row["admin"]`` directly. Only columns
    actually present are applied now, which makes a deliberately narrow file
    -- a list of emails and new states, say -- a supported way to work.
    """
    member = make_member(state="noob", rfid="TAG-KEEP")

    result = import_rows([member.user.email, "active"], headers=["email", "state"])

    assert row_results(result) == [("update", [])]
    member.refresh_from_db()
    assert member.state == "active"
    assert (member.first_name, member.screen_name) == ("Test", "member1")
    assert member.rfid == "TAG-KEEP"


def test_an_unrecognised_state_is_rejected_before_anything_is_written():
    """``state`` has choices, but the database does not enforce them.

    Without this check the value would be stored happily, leaving a member in
    a state nothing else in the system knows how to read.
    """
    from profile.models import User

    result = import_rows(member_row("bad@example.com", state="platinum"))

    (row,) = result.rows
    assert row.import_type == "invalid"
    assert "platinum" in str(row.validation_error)
    assert not User.objects.filter(email="bad@example.com").exists()


# --------------------------------------------------------------------------
# Import: rows that should not be written
# --------------------------------------------------------------------------


def test_the_placeholder_row_is_skipped_without_creating_anything():
    """The skip guard used to run too late to prevent anything.

    ``skip_row`` excludes "default@example.com" -- the fixture account --
    but django-import-export calls ``before_import_row`` first and
    ``skip_row`` some thirty lines later, so the hook had already created the
    user and profile. The profile write happens in ``after_save_instance``
    now, which a skipped row never reaches.
    """
    from profile.models import Profile, User

    result = import_rows(member_row("default@example.com", state="active"))

    assert row_results(result) == [("skip", [])]
    assert not User.objects.filter(email="default@example.com").exists()
    assert not Profile.objects.exists()


def test_a_dry_run_writes_nothing():
    from profile.models import Profile, User

    resource().import_data(
        dataset(member_row("dry@example.com", state="active")),
        dry_run=True,
        raise_errors=False,
    )

    assert not User.objects.filter(email="dry@example.com").exists()
    assert not Profile.objects.exists()
