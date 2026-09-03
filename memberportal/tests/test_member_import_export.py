"""Characterisation: the member import/export offered by the Django admin.

``UserResource`` in ``profile/admin.py`` backs the "Import" and "Export"
buttons on the user list. It is the only bulk write path in the system, and
the only one where a mistake corrupts the member roster silently rather than
raising at an endpoint.

The awkwardness it works around is real: ``User`` holds only ``email``,
``staff`` and ``admin``; every human detail (name, screen name, RFID tag,
membership state) lives on the one-to-one ``Profile``. The resource bridges
that gap with a declared field per profile column, and each of those fields
is given a ``ForeignKeyWidget(Profile, "<column>")`` -- despite none of them
being a foreign key.

That widget choice is what these tests are mostly about. On export it is
inert, because ``dehydrate_*`` methods take over. On import it is not: the
widget's job is to turn a cell into a related object, so it runs
``Profile.objects.get(<column>=<cell>)``. The result is an import path that
only succeeds by coincidence, and a round-trip that cannot survive its own
export -- see ``test_a_freshly_exported_file_cannot_be_imported_back``.
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


def row_errors(result):
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


def test_the_export_always_loses_the_screen_name(make_member):
    """DEFECT, pinned: the dehydrate hook for this column is misspelled.

    ``UserResource`` defines ``dehydrate_screen_name_name`` -- one ``_name``
    too many. django-import-export dispatches on the exact name
    ``dehydrate_<field>``, so the method never runs and nothing reads
    ``profile.screen_name``. The field falls back to its declared
    ``attribute="screen_name"``, which is looked up on ``User``, where no
    such attribute exists, and the cell comes out empty.

    Every member export ever taken from this system has a blank screen_name
    column.
    """
    member = make_member(state="active", rfid="EXPORT-3")
    assert member.screen_name == "member1"

    (row,) = resource().export().dict

    assert row["screen_name"] == ""


def test_a_user_with_no_profile_exports_blanks_instead_of_failing(db):
    """The ``try/except`` in each dehydrate hook, doing its one job.

    A profile-less ``User`` is reachable -- ``User.__str__`` guards against
    it too -- so the export declines to raise on one.
    """
    from profile.models import User

    User.objects.create(email="ghost@example.com")

    (row,) = resource().export().dict

    assert row["email"] == "ghost@example.com"
    assert row["first_name"] == ""
    assert row["last_name"] == ""
    assert row["rfid"] is None
    # Not "" -- the fallback for a missing profile is the model's own default.
    assert row["state"] == "noob"


# --------------------------------------------------------------------------
# Import: the widget lookup
# --------------------------------------------------------------------------


def test_a_row_imports_only_when_its_state_is_the_model_default():
    """DEFECT, pinned: ``state`` is matched against other rows, not stored.

    ``before_import_row`` creates the ``Profile`` without passing ``state``,
    so the new row always starts at the model default of "noob". The
    ``state`` field then runs ``Profile.objects.get(state=<cell>)`` through
    its ForeignKeyWidget. Those two agree only when the cell says "noob",
    which is why an import appears to work at all.
    """
    result = import_rows(member_row("noob@example.com", state="noob"))

    assert row_errors(result) == [("update", [])]


def test_importing_an_active_member_fails_outright():
    """DEFECT, pinned: the same lookup, for any state a real member has.

    Nothing was created: the row raised, and the surrounding transaction
    rolled the ``before_import_row`` side effects back with it.
    """
    from profile.models import User

    result = import_rows(member_row("active@example.com", state="active"))

    (import_type, errors) = row_errors(result)[0]
    assert import_type == "error"
    assert "Profile matching query does not exist" in errors[0]
    assert not User.objects.filter(email="active@example.com").exists()


def test_the_second_member_sharing_a_column_value_collides():
    """DEFECT, pinned: ``.get()`` on a non-unique column, so two is too many.

    The first row imports and leaves a "noob" profile behind. The second row
    looks up ``Profile.objects.get(state="noob")`` and now matches both.
    The same holds for any two members sharing a first name, last name or
    screen name -- none of those columns is unique either.
    """
    result = import_rows(
        member_row("first@example.com", first_name="Cee", screen_name="cee", rfid="T3"),
        member_row(
            "second@example.com", first_name="Dee", screen_name="dee", rfid="T4"
        ),
    )

    assert row_errors(result)[0] == ("update", [])
    (import_type, errors) = row_errors(result)[1]
    assert import_type == "error"
    assert "returned more than one Profile" in errors[0]


def test_a_freshly_exported_file_cannot_be_imported_back(make_member):
    """DEFECT, pinned: the round-trip this feature exists for does not work.

    Export one active member, drop them, import the exact file back. The
    ``state`` cell says "active" and the lookup fails, so the restore
    produces nothing. Taking a backup through this path and restoring it is
    the headline use of an import/export button, and it has never worked for
    a member in any state but "noob".
    """
    from profile.models import User

    make_member(state="active", rfid="ROUND-1")
    exported = resource().export()

    User.objects.all().delete()
    result = resource().import_data(exported, dry_run=False, raise_errors=False)

    assert row_errors(result)[0][0] == "error"
    assert not User.objects.exists()


# --------------------------------------------------------------------------
# Import: what a successful row actually writes
# --------------------------------------------------------------------------


def test_a_new_member_is_created_by_the_pre_import_hook():
    from profile.models import Profile, User

    import_rows(member_row("new@example.com", state="noob"))

    user = User.objects.get(email="new@example.com")
    assert user.email_verified is True
    profile = Profile.objects.get(user=user)
    assert (profile.first_name, profile.last_name) == ("Alice", "Anders")
    assert (profile.screen_name, profile.rfid) == ("alice", "TAG-A")


def test_a_created_member_is_reported_as_an_update(make_member):
    """DEFECT, pinned: the import summary can never say "new".

    ``before_import_row`` runs before django-import-export looks the
    instance up, so by the time it does, the user it would have called new
    already exists and the row is classified as an update. The admin's
    import preview therefore reports 0 created for any file, however many
    members it adds.
    """
    result = import_rows(member_row("counted@example.com", state="noob"))

    assert row_errors(result) == [("update", [])]


def test_an_existing_member_is_left_completely_untouched(make_member):
    """DEFECT, pinned: import can create a member but never update one.

    ``before_import_row`` writes the profile columns only on the ``created``
    branch of its ``get_or_create``. For a member who already exists, every
    profile column in the row is discarded. ``staff`` and ``admin`` are real
    ``User`` fields and would be written -- but only if the row survives the
    widget lookup first, which for an existing member it generally does not.
    """
    member = make_member(state="noob", rfid="TAG-OLD")

    import_rows(
        member_row(
            member.user.email,
            staff="1",
            first_name="NewFirst",
            last_name="NewLast",
            screen_name="newscreen",
            rfid="TAG-NEW",
            state="noob",
        )
    )

    member.refresh_from_db()
    member.user.refresh_from_db()
    assert (member.first_name, member.last_name) == ("Test", "Member1")
    assert (member.screen_name, member.rfid) == ("member1", "TAG-OLD")
    assert member.user.staff is False


def test_the_placeholder_row_is_skipped_after_it_has_already_been_created():
    """DEFECT, pinned: the skip guard runs too late to prevent anything.

    ``skip_row`` excludes "default@example.com" -- the fixture account -- but
    django-import-export calls ``before_import_row`` first and ``skip_row``
    some thirty lines later, once an instance has been built. The user and
    profile are created by the hook regardless, and the row then reports
    itself as skipped.
    """
    from profile.models import Profile, User

    result = import_rows(member_row("default@example.com", state="noob"))

    assert row_errors(result) == [("skip", [])]
    assert User.objects.filter(email="default@example.com").exists()
    assert Profile.objects.filter(first_name="Alice").exists()


def test_a_file_missing_a_column_fails_on_the_row_not_the_file():
    """DEFECT, pinned: ``before_import_row`` indexes instead of getting.

    ``row["admin"]`` raises ``KeyError`` for a file that omits the column.
    django-import-export catches it per row, so a partial file does not fail
    up front -- it fails once per row, with a bare column name as the whole
    error message.
    """
    result = import_rows(
        ["bob@example.com", "Bob", "Bee", "bob", "TAG-B"],
        headers=["email", "first_name", "last_name", "screen_name", "rfid"],
    )

    (import_type, errors) = row_errors(result)[0]
    assert import_type == "error"
    assert errors == ["KeyError('admin')"]


def test_a_dry_run_writes_nothing():
    """The transaction wrapper holds, so the admin's preview is safe.

    Worth pinning precisely because ``before_import_row`` writes outside
    django-import-export's own save path: it is the rollback, not the
    resource, that keeps a preview from touching the database.
    """
    from profile.models import User

    resource().import_data(
        dataset(member_row("dry@example.com", state="noob")),
        dry_run=True,
        raise_errors=False,
    )

    assert not User.objects.filter(email="dry@example.com").exists()
