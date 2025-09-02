from django.db import models
from django.utils.translation import gettext_lazy as _


class AccessScope(models.TextChoices):
    ORG_ALL = "ORG_ALL", _("All members of the workflow's organization")
    RESTRICTED = "RESTRICTED", _("Restricted to allowed users and/or roles")


class FailPolicy(models.TextChoices):
    CONTINUE = "CONTINUE", _("Continue on failure")
    FAIL_FAST = "FAIL_FAST", _("Fail fast")


class TriggerType(models.TextChoices):
    MANUAL = "MANUAL", _("Manual")
    API = "API", _("API")
    SCHEDULE = "SCHEDULE", _("Schedule")
    GITHUB_APP = "GITHUB_APP", _("GitHub App")
