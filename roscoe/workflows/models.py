from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.users.models import Organization, User


class Workflow(TimeStampedModel):
    """
    Reusable, versioned definition of a sequence of validation steps.
    """

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "slug",
                    "version",
                ],
                name="uq_workflow_org_slug_version",
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

    # Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": _("Name is required.")})

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @transaction.atomic
    def clone_to_new_version(self, user) -> Workflow:
        """
        Create an identical workflow with version+1 and copied steps.
        Locks old version.
        """
        latest_version = (
            Workflow.objects.filter(org=self.org, slug=self.slug)
            .exclude(pk=self.pk)
            .aggregate(models.Max("version"))["version__max"]
            or self.version
        )
        new = Workflow.objects.create(
            org=self.org,
            user=user,
            name=self.name,
            slug=self.slug,
            version=latest_version + 1,
            is_locked=False,
        )
        steps = []
        for step in self.steps.all().order_by("order"):
            step.pk = None
            step.workflow = new
            steps.append(step)
        WorkflowStep.objects.bulk_create(steps)
        self.is_locked = True
        self.save(update_fields=["is_locked"])
        return new


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
        "validations.Validator",
        on_delete=models.PROTECT,
    )

    ruleset = models.ForeignKey(
        "validations.Ruleset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    # Optional per-step config (e.g., severity thresholds, mapping)
    config = models.JSONField(default=dict, blank=True)

    def clean(self):
        
        super().clean()
        
        if (
            WorkflowStep.objects.filter(workflow=self.workflow, order=self.order)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError({"order": _("Order already used in this workflow.")})
        
        if self.ruleset and self.ruleset.type != self.validator.type:
            raise ValidationError(
                {"ruleset": _("Ruleset type must match validator type.")}
            )
