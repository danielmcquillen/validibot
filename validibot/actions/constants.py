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

    SIGNED_CERTIFICATE = "SIGNED_CERTIFICATE", _("Signed certificate")
