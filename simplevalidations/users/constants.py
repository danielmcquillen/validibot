from django.db import models
from django.utils.translation import gettext_lazy as _


class RoleCode(models.TextChoices):
    """
    Enum for user roles within an organization.
    """

    ADMIN = "ADMIN", _("Admin")
    OWNER = "OWNER", _("Owner")
    AUTHOR = "AUTHOR", _("Author")
    EXECUTOR = "EXECUTOR", _("Executor")
    VIEWER = "VIEWER", _("Viewer")
