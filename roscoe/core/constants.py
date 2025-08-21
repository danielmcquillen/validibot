from django.db import models
from django.utils.translation import gettext_lazy as _


class RequestType(models.TextChoices):
    API = "api", _("API")
    UI = "ui", _("UI")
    GITHUB_APP = "github_app", _("GitHub App")
