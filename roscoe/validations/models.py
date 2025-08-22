from __future__ import annotations

import uuid

import slugify
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.documents.models import Submission
from roscoe.projects.models import Project
from roscoe.users.models import Organization, User
from roscoe.validations.constants import (
    JobStatus,
    RulesetType,
    Severity,
    StepStatus,
    ValidationType,
)
from roscoe.workflow.models import Workflow, WorkflowStep


class Ruleset(TimeStampedModel):
    """
    Schema or rule bundle (JSON Schema, XSD, YAML rules, etc.)
    Can be global (org=None) or org-private.
    """

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "org",
                    "type",
                ]
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "type",
                    "name",
                    "version",
                ],
                name="uq_ruleset_org_type_name_version",
            )
        ]

    org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="rulesets",
    )

    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rulesets",
        help_text=_("The user who created this ruleset."),
    )

    name = models.CharField(max_length=200)

    type = models.CharField(
        max_length=40,
        choices=RulesetType.choices,
        help_text=_("Type of validation ruleset, e.g. 'json_schema', 'xml_schema'"),
    )

    version = models.CharField(max_length=40, blank=True, default="")

    file = models.FileField(upload_to="rulesets/")  # or TextField for inline content

    metadata = models.JSONField(default=dict, blank=True)


class Validator(TimeStampedModel):
    """
    A pluggable validator 'type' and version.
    Examples:
      kind='json_schema', version='2020-12'
      kind='xml_schema', version='1.0'
      kind='energyplus', version='23.1'
    """

    class Meta:
        unique_together = [
            (
                "slug",
                "version",
            )
        ]
        indexes = [
            models.Index(
                fields=[
                    "type",
                    "slug",
                ]
            )
        ]

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_(
            "A unique identifier for the validator, used in URLs."
        ),  # e.g. "json-2020-12", "eplus-23-1"
    )

    name = models.CharField(
        max_length=120,
        null=False,
        blank=False,
    )  # display label

    type = models.CharField(
        max_length=40,
        choices=ValidationType.choices,
        null=False,
        blank=False,
    )

    version = models.PositiveIntegerField(
        help_text=_("Version of the validator, e.g. 1, 2, 3")
    )

    is_public = models.BooleanField(default=True)  # false for org-private validators

    default_ruleset = models.ForeignKey(
        Ruleset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    def __str__(self):
        return f"{self.type} {self.slug} v{self.version}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(f"{self.name}")
        super().save(*args, **kwargs)


class ValidationRun(TimeStampedModel):
    """
    One execution of a Submission through a specific Workflow version.

    Not normalized, as Workflow has a link to org and user,
    but we store org/project/user here to preserve historical truth,
    query performance and access control.

    """

    class Meta:
        indexes = [
            models.Index(fields=["org", "project", "workflow", "created"]),
            models.Index(fields=["status", "created"]),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="validation_runs",
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validation_runs",
    )

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validation_runs",
    )

    submission = models.ForeignKey(
        Submission, on_delete=models.CASCADE, related_name="runs"
    )
    workflow = models.ForeignKey(
        Workflow, on_delete=models.PROTECT, related_name="runs"
    )

    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING
    )

    started_at = models.DateTimeField(null=True, blank=True)

    ended_at = models.DateTimeField(null=True, blank=True)

    duration_ms = models.BigIntegerField(default=0)

    summary = models.JSONField(default=dict, blank=True)  # counts, pass/fail, etc.

    error = models.TextField(blank=True, default="")  # terminal error/trace if any

    resolved_config = models.JSONField(
        default=dict, blank=True
    )  # effective per-run config snapshot


class ValidationStepRun(TimeStampedModel):
    """
    Execution of a single WorkflowStep within a ValidationRun.
    """

    class Meta:
        unique_together = [("run", "step_order")]
        indexes = [models.Index(fields=["run", "status"])]

    run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="step_runs",
    )

    step = models.ForeignKey(
        WorkflowStep,
        on_delete=models.PROTECT,
        related_name="+",
    )

    step_order = (
        models.PositiveIntegerField()
    )  # denormalized copy of step.order for quick lookup

    status = models.CharField(
        max_length=16,
        choices=StepStatus.choices,
        default=StepStatus.PENDING,
    )

    started_at = models.DateTimeField(null=True, blank=True)

    ended_at = models.DateTimeField(null=True, blank=True)

    duration_ms = models.BigIntegerField(default=0)

    output = models.JSONField(
        default=dict, blank=True
    )  # machine output (validator-specific)

    error = models.TextField(blank=True, default="")

    def clean(self):
        
        super().clean()
        
        if self.step and self.run and self.step.workflow_id != self.run.workflow_id:
            raise ValidationError(
                {"step": _("Step must belong to the run's workflow.")}
            )
            
        if self.step and self.step_order and self.step.order != self.step_order:
            raise ValidationError({"step_order": _("Must equal WorkflowStep.order.")})


class ValidationFinding(TimeStampedModel):
    """
    Normalized issues produced by step runs for efficient filtering/pagination.
    """

    class Meta:
        indexes = [
            models.Index(fields=["run", "severity"]),
            models.Index(fields=["run", "code"]),
        ]

    run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="findings",
    )

    step_run = models.ForeignKey(
        ValidationStepRun,
        on_delete=models.CASCADE,
        related_name="findings",
    )

    severity = models.CharField(max_length=16, choices=Severity.choices)

    code = models.CharField(
        max_length=64, blank=True, default=""
    )  # e.g. "json.schema.required"

    message = models.TextField()

    path = models.CharField(
        max_length=512, blank=True, default=""
    )  # JSON Pointer/XPath/etc.

    meta = models.JSONField(default=dict, blank=True)


def artifact_upload_to(instance, filename: str) -> str:
    return f"artifacts/org-{instance.org_id}/runs/{instance.run_id}/{uuid.uuid4().hex}/{filename}"


class Artifact(TimeStampedModel):
    """
    Files emitted during a run (logs, reports, transformed docs, E+ outputs).
    """

    class Meta:
        indexes = [models.Index(fields=["run", "created"])]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )

    run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )

    label = models.CharField(max_length=120)

    content_type = models.CharField(max_length=128, blank=True, default="")

    file = models.FileField(upload_to=artifact_upload_to)

    size_bytes = models.BigIntegerField(default=0)

    def clean(self):
        super().clean()
        if self.org_id and self.run_id and self.org_id != self.run.org_id:
            raise ValidationError({"org": _("Artifact org must match run org.")})
