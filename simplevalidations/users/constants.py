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


class PermissionCode(models.TextChoices):
    """
    Canonical permission codes used by the org-scoped RBAC layer.
    """

    WORKFLOW_LAUNCH = "workflow_launch", _("Start workflow runs")
    VALIDATION_RESULTS_VIEW_ALL = (
        "validation_results_view_all",
        _("View all validation results"),
    )
    VALIDATION_RESULTS_VIEW_OWN = (
        "validation_results_view_own",
        _("View validation results for own runs"),
    )
    WORKFLOW_VIEW = "workflow_view", _("View workflow definitions and metadata")
    WORKFLOW_EDIT = "workflow_edit", _("Create or edit workflows")
    VALIDATOR_VIEW = "validator_view", _("View validators and catalog entries")
    VALIDATOR_EDIT = "validator_edit", _("Create or edit validators")
    ANALYTICS_VIEW = "analytics_view", _("View analytics and reporting dashboards")
    ANALYTICS_REVIEW = "analytics_review", _("Review or approve analytics outputs")
    ADMIN_MANAGE_ORG = "admin_manage_org", _("Manage organization users and roles")
