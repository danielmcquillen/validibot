from django.db import models
from django.utils.translation import gettext_lazy as _


class ActionCategoryType(models.TextChoices):
    """High-level grouping for non-validation workflow actions."""

    INTEGRATION = "INTEGRATION", _("Integration")
    CERTIFICATION = "CERTIFICATION", _("Certification")


class IntegrationActionType(models.TextChoices):
    """Supported integration actions that can be attached to a workflow."""

    SLACK_MESSAGE = "SLACK_MESSAGE", _("Slack message")


class CertificationActionType(models.TextChoices):
    """Supported certification actions that can be attached to a workflow."""

    SIGNED_CREDENTIAL = "SIGNED_CREDENTIAL", _("Signed credential")


class ActionFailureMode(models.TextChoices):
    """How an action step failure affects the overall workflow run.

    BLOCKING:
        A failed action fails the workflow run.  This is the default
        for most actions — if the action matters enough to be in the
        workflow, its failure should be visible.

    ADVISORY:
        A failed action records step failure and diagnostics, but the
        run may still succeed.  Use this for actions where failure
        should not block the primary validation outcome, such as
        credential issuance or optional notifications.
    """

    BLOCKING = "BLOCKING", _("Blocking")
    ADVISORY = "ADVISORY", _("Advisory")
