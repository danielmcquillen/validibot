from django.db import models
from django.utils.translation import gettext_lazy as _


class RoleCode(models.TextChoices):
    """
    Enum for user roles within an organization.
    """

    # Owner of an organization.
    OWNER = "OWNER", _("Owner")

    # Admin of an organization.
    ADMIN = "ADMIN", _("Admin")

    # Author role with permissions to create and manage workflows within the org.
    AUTHOR = "AUTHOR", _("Author")

    # Executor role with permissions to run validations within the org.
    EXECUTOR = "EXECUTOR", _("Executor")

    # Viewer role with read-only access to workflows and reports within the org.
    VIEWER = "VIEWER", _("Viewer")
