from django.db import models
from django.utils.translation import gettext_lazy as _


class RoleCode(models.TextChoices):
    """
    Enum for user roles within an organization.
    """

    OWNER = "OWNER", _("Owner")
    AUTHOR = "AUTHOR", _("Author")
    EXECUTE = "EXECUTE", _("Execute")
    VIEWER = "VIEWER", _("Viewer")
