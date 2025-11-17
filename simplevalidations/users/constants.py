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

    # Reviewer role with permissions to review validation results within the org.
    RESULTS_VIEWER = (
        "RESULTS_VIEWER",
        _("Validation Results Viewer"),
    )

    # Viewer role with read-only access to workflow view and reports within the org.
    # This provides a more detailed view of the workflow...more than what is shown
    # in the public view. But no edit or execution permissions are granted.
    WORKFLOW_VIEWER = "WORKFLOW_VIEWER", _("Workflow Viewer")
