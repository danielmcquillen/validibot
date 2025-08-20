import uuid

from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.users.models import Organization, User


class Workflow(TimeStampedModel):
    """
    Reusable, versioned definition of a sequence of validation steps.
    """

    class Meta:
        unique_together = [
            (
                "org",
                "slug",
                "version",
            )
        ]
        ordering = [
            "slug",
            "-version",
        ]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="workflows",
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="workflows",
        help_text=_("The user who created this workflow."),
    )

    name = models.CharField(
        max_length=200,
        blank=False,
        null=False,
        help_text=_("Name of the workflow, e.g. 'My Workflow'"),
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique identifier for the workflow."),
    )

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_("A unique identifier for the workflow, used in URLs."),
    )

    version = models.PositiveIntegerField()

    is_locked = models.BooleanField(
        default=False,
    )

    def clean(self):
        from django.core.exceptions import ValidationError

        if not self.name or not self.name.strip():
            raise ValidationError({"name": _("Name is required.")})

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class WorkflowStep(TimeStampedModel):
    """
    One step in a workflow, ordered. Linear for MVP.
    """

    class Meta:
        unique_together = [
            (
                "workflow",
                "order",
            )
        ]
        ordering = ["order"]

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="steps",
    )

    order = models.PositiveIntegerField()  # 10,20,30... leave gaps for inserts

    name = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )

    validator = models.ForeignKey(
        "validators.Validator",
        on_delete=models.PROTECT,
    )

    ruleset = models.ForeignKey(
        "validators.Ruleset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    # Optional per-step config (e.g., severity thresholds, mapping)
    config = models.JSONField(default=dict, blank=True)
