from django.db import models
from django.utils.translation import gettext_lazy as _


class RequestType(models.TextChoices):
    API = "API", _("API")
    UI = "UI", _("UI")
    GITHUB_APP = "GITHUB_APP", _("GitHub App")


class InviteStatus(models.TextChoices):
    """
    Shared status choices for all invite types.

    Used by MemberInvite, WorkflowInvite, and GuestInvite to standardize
    invite lifecycle states.
    """

    PENDING = "PENDING", _("Pending")
    ACCEPTED = "ACCEPTED", _("Accepted")
    DECLINED = "DECLINED", _("Declined")
    CANCELED = "CANCELED", _("Canceled")
    EXPIRED = "EXPIRED", _("Expired")
