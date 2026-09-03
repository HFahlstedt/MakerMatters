from django.contrib import admin
from django.core.exceptions import ValidationError
from .models import *
from import_export.admin import ImportExportModelAdmin
from import_export import resources, fields


class UserResource(resources.ModelResource):
    """Bulk member import and export, behind the admin's Import/Export buttons.

    ``User`` carries only the account columns -- email, staff, admin. Every
    human detail (name, screen name, RFID tag, membership state) lives on the
    one-to-one ``Profile``, so this resource has to span two tables.

    It does that by declaring each profile column with no ``attribute``.
    django-import-export skips a field with no attribute on the way in,
    because there is nothing on ``User`` to write it to, which leaves both
    directions to the hooks below: ``dehydrate_*`` reads a column for export,
    and ``after_save_instance`` writes the whole set back to the profile.

    Writing the profile there, rather than up front in ``before_import_row``,
    is what keeps a skipped or failed row from leaving a half-made member
    behind -- the hook runs only for rows that were actually saved.

    Note a member created this way has no usable password and has to go
    through a password reset before they can log in.
    """

    #: The profile columns this resource spans, mapped to the value the export
    #: falls back to for a user with no profile at all. That is reachable --
    #: ``User.__str__`` guards against it too -- so exporting one is not worth
    #: failing a whole download over.
    PROFILE_COLUMNS = {
        "first_name": "",
        "last_name": "",
        "screen_name": "",
        "rfid": None,
        "state": "noob",
    }

    # Declared without an attribute: see the class docstring.
    first_name = fields.Field(column_name="first_name")
    last_name = fields.Field(column_name="last_name")
    screen_name = fields.Field(column_name="screen_name")
    rfid = fields.Field(column_name="rfid")
    state = fields.Field(column_name="state")

    @classmethod
    def profile_column(cls, user, column):
        try:
            profile = user.profile
        except Profile.DoesNotExist:
            return cls.PROFILE_COLUMNS[column]

        return getattr(profile, column)

    # One hook per column, because django-import-export dispatches on the
    # exact name ``dehydrate_<field>``.
    def dehydrate_first_name(self, user):
        return self.profile_column(user, "first_name")

    def dehydrate_last_name(self, user):
        return self.profile_column(user, "last_name")

    def dehydrate_screen_name(self, user):
        return self.profile_column(user, "screen_name")

    def dehydrate_rfid(self, user):
        return self.profile_column(user, "rfid")

    def dehydrate_state(self, user):
        return self.profile_column(user, "state")

    def skip_row(self, instance, original, row, import_validation_errors=None):
        """Never import the fixture account shipped with the project."""
        return row.get("email") == "default@example.com"

    def before_save_instance(self, instance, row, **kwargs):
        """Reject a bad membership state before anything is written.

        ``state`` has choices but they are not enforced by the database, so
        an unrecognised value would otherwise be stored happily and leave a
        member in a state nothing else in the system knows how to handle.
        Raising here, rather than after the save, means the row fails whole.
        """
        state = row.get("state")

        if state is not None and state not in dict(Profile.STATES):
            raise ValidationError(
                {"state": f"{state!r} is not one of {sorted(dict(Profile.STATES))}."}
            )

    def after_save_instance(self, instance, row, **kwargs):
        """Apply the profile columns the file actually names.

        Only columns present in the row are touched, so a partial file -- a
        list of emails and new states, say -- updates those and leaves the
        rest of each member alone.
        """
        profile, _created = Profile.objects.get_or_create(user=instance)

        for column in self.PROFILE_COLUMNS:
            if column in row:
                setattr(profile, column, self.clean_profile_value(column, row[column]))

        profile.save()

    @staticmethod
    def clean_profile_value(column, value):
        if column == "rfid":
            # The column is unique, so an absent tag has to be NULL: two
            # members holding "" would collide with each other.
            return value or None

        return value

    class Meta:
        model = User
        import_id_fields = ["email"]
        # NOTE: every profile column must be listed explicitly. Up to
        # django-import-export 3.x, fields declared as attributes on the
        # Resource bypassed this whitelist; from 4.0 the whitelist applies to
        # them too and unlisted ones are dropped (upstream #1693). Without
        # "state" here, the member export silently loses its state column.
        fields = (
            "email",
            "staff",
            "admin",
            "first_name",
            "last_name",
            "screen_name",
            "rfid",
            "state",
        )


@admin.register(User)
class AdminLogAdmin(ImportExportModelAdmin, admin.ModelAdmin):
    resource_class = UserResource
    pass


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    readonly_fields = ("created", "subscription_first_created")
    pass


@admin.register(UserEventLog)
class UserEventLogAdmin(admin.ModelAdmin):
    readonly_fields = ("date",)


@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    readonly_fields = ("date",)
