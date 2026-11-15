import json
import logging
from django.utils.translation import gettext_lazy as _
from oidc_provider.lib.claims import ScopeClaims
from constance import config

logger = logging.getLogger("oidc_provider")


def userinfo(claims, user):
    # Populate claims dict. A claim the member has not filled in is left out,
    # as OpenID Connect intends, rather than sent as a made-up placeholder that
    # a relying party would store as the member's real name or number.
    profile = user.profile
    optional = {
        "name": (user.get_full_name() or "").strip(),
        "given_name": profile.first_name,
        "family_name": profile.last_name,
        "nickname": profile.screen_name,
        "preferred_username": profile.screen_name,
        "phone_number": profile.phone,
    }
    for claim, value in optional.items():
        if value:
            claims[claim] = value
        else:
            claims.pop(claim, None)

    claims["email"] = user.email
    claims["email_verified"] = user.email_verified
    claims["phone_number_verified"] = False
    claims["updated_at"] = user.profile.modified.isoformat()

    return claims


class CustomScopeClaims(ScopeClaims):
    info_membershipinfo = (
        _("Membership Info"),
        _(
            "Current membership status, and other membership information like permissions/groups."
        ),
    )

    info_vikunja_teams = (
        _("Vikunja Teams"),
        _("Vikunja teams all members should be added to automatically."),
    )

    def scope_membershipinfo(self):
        groups = []
        state = self.user.profile.state
        subscription_state = self.user.profile.subscription_status
        subscriptionActive = subscription_state in ["active", "cancelling"]
        firstSubscribed = self.user.profile.subscription_first_created
        firstSubscribed = firstSubscribed.isoformat() if firstSubscribed else None

        if self.user.is_staff:
            groups.append("staff")

        if self.user.is_admin:
            groups.append("admin")

        if self.user.is_superuser:
            groups.append("superuser")

        if self.user.profile.state == "active":
            groups.append("active")

        return {
            "state": state,
            "active": state == "active",
            "subscriptionState": subscription_state,
            "subscriptionActive": subscriptionActive,
            "firstSubscribedDate": firstSubscribed,
            "groups": groups,
        }

    def scope_vikunja_teams(self):
        if config.VIKUNJA_TEAMS:
            try:
                teams = json.loads(config.VIKUNJA_TEAMS)
                if not isinstance(teams, list):
                    logger.error(
                        "VIKUNJA_TEAMS is not a JSON array and the Vikunja teams claim wasn't added."
                    )
                    return {}
                if self.user.profile.state == "active":
                    return {
                        "vikunja_groups": teams,
                    }
                else:
                    return {
                        "vikunja_groups": [],
                    }
            except json.JSONDecodeError:
                logger.error(
                    "VIKUNJA_TEAMS is not a valid JSON object and the Vikunja teams claim wasn't added."
                )

        return {}
