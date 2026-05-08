from django.db import models
from django.utils.translation import gettext_lazy as _


class RoleCode(models.TextChoices):
    """
    Enum for user roles within an organization.
    """

    # Owner of an organization. All ADMIN permissions plus organization settings.
    OWNER = "OWNER", _("Owner")

    # Admin of an organization. All AUTHOR permissions plus user and org management.
    ADMIN = "ADMIN", _("Admin")

    # Author role with all EXECUTOR permission as well as the ability to create
    # and edit workflows in an org.
    AUTHOR = "AUTHOR", _("Author")

    # Executor role with all viewer permissions plus the ability
    # to to run validations for workflows within an org.
    EXECUTOR = "EXECUTOR", _("Executor")

    # Analytics-focused read-only role.
    ANALYTICS_VIEWER = "ANALYTICS_VIEWER", _("Analytics Viewer")

    # Viewer role with read-only access to workflow view and reports within the org.
    # This provides a more detailed view of the workflow...more than what is shown
    # in the public view. But no edit or execution permissions are granted.
    WORKFLOW_VIEWER = "WORKFLOW_VIEWER", _("Workflow Viewer")

    # Reviewer role with permissions to review validation results within the org.
    VALIDATION_RESULTS_VIEWER = (
        "VALIDATION_RESULTS_VIEWER",
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
    GUEST_INVITE = "guest_invite", _("Send guest invites for this organization")


class UserKindGroup(models.TextChoices):
    """Names of the user-kind classifier Django Groups.

    Two groups label every user as either a basic user or a guest:

    * ``Basic Users`` — regular accounts. Their per-org capabilities are
      governed by ``Membership`` roles plus the per-org RBAC backend.
    * ``Guests`` — external collaborators with workflow access grants.
      Cannot join an organization as a member without an explicit
      superuser-run promotion (see :mod:`~validibot.users.user_kind` and
      the ``promote_user`` management command).

    These are CLASSIFICATION groups, not permission containers — they
    carry no permissions of their own. Every user is in exactly one.

    Values are human-readable strings ("Basic Users", "Guests") because
    they live in ``auth_group.name`` and surface directly in the Django
    admin UI; the uppercase-values convention used by ``RoleCode`` /
    ``PermissionCode`` does not apply to Django's own ``auth_group``
    rows. Use ``UserKindGroup.BASIC.value`` to look up the group by
    name (or just ``UserKindGroup.BASIC`` since ``TextChoices`` members
    compare equal to their string value).
    """

    BASIC = "Basic Users", _("Basic Users")
    GUEST = "Guests", _("Guests")


# Reserved organization slugs that can only be created by superusers.
# These are used for integration testing and system purposes.
RESERVED_ORG_SLUGS = frozenset(
    {
        "test-org",
        "test",
        "admin",
        "system",
        "validibot",
    }
)
