from django.db import models
from django.utils.translation import gettext_lazy as _


class RoleCode(models.TextChoices):
    """
    Enum for user roles within an organization.
    """

    # Owner of an organization. All ADMIN permissions plus billing and
    # subscription management.
    OWNER = "OWNER", _("Owner")

    # Admin of an organization. All AUTHOR permissions plus user and org management.
    ADMIN = "ADMIN", _("Admin")

    # Author role with all EXECUTOR permission as well as the ability to create
    # and edit workflows in an org.
    AUTHOR = "AUTHOR", _("Author")

    # Executor role with all viewer permissions plus the ability
    # to to run validations for workflows within an org.
    EXECUTOR = "EXECUTOR", _("Executor")

    # Viewer role with read-only access to workflow view and reports within the org.
    # This provides a more detailed view of the workflow...more than what is shown
    # in the public view. But no edit or execution permissions are granted.
    WORKFLOW_VIEWER = "WORKFLOW_VIEWER", _("Workflow Viewer")

    # Reviewer role with permissions to review validation results within the org.
    RESULTS_VIEWER = (
        "RESULTS_VIEWER",
        _("Validation Results Viewer"),
    )
