from django.db import models
from django.utils.translation import gettext_lazy as _


class FailPolicy(models.TextChoices):
    CONTINUE = "continue", _("Continue on failure")
    FAIL_FAST = "fail_fast", _("Fail fast")


class TriggerType(models.TextChoices):
    MANUAL = "manual", _("Manual")
    API = "api", _("API")
    SCHEDULE = "schedule", _("Schedule")
    GITHUB_APP = "github_app", _("GitHub App")
