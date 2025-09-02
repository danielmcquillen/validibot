from django.db import models
from django.utils.translation import gettext_lazy as _


class RequestType(models.TextChoices):
    API = "API", _("API")
    UI = "UI", _("UI")
    GITHUB_APP = "GITHUB_APP", _("GitHub App")
