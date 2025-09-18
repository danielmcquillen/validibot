from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel
from slugify import slugify

from roscoe.projects.models import Project
from roscoe.submissions.models import Submission
from roscoe.users.models import Organization
from roscoe.users.models import User
from roscoe.validations.constants import RulesetType
from roscoe.validations.constants import Severity
from roscoe.validations.constants import StepStatus
from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.constants import ValidationType
from roscoe.validations.constants import XMLSchemaType
from roscoe.workflows.models import Workflow
from roscoe.workflows.models import WorkflowStep


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
                    "ruleset_type",
                ],
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "ruleset_type",
                    "name",
                    "version",
                ],
                name="uq_ruleset_org_ruleset_type_name_version",
            ),
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

    ruleset_type = models.CharField(
        max_length=40,
        choices=RulesetType.choices,
        help_text=_("Type of validation ruleset, e.g. 'json_schema', 'xml_schema'"),
    )

    version = models.CharField(
        max_length=40,
        blank=True,
        default="",
    )

    file = models.FileField(upload_to="rulesets/")  # or TextField for inline content

    metadata = models.JSONField(default=dict, blank=True)

    def clean(self):
        super().clean()

        # Validate XML schema_type when this ruleset is for XML
        if self.ruleset_type == RulesetType.XML_SCHEMA:
            meta = self.metadata or {}
            schema_type = meta.get("schema_type")
            if not schema_type:
                # Default to XSD if not specified
                meta["schema_type"] = XMLSchemaType.XSD.value
                self.metadata = meta
                return
            schema_type = str(schema_type).strip().upper()
            # Compare against TextChoices values (strings)
            if schema_type not in set(XMLSchemaType.values):
                raise ValidationError(
                    {
                        "metadata": _("Schema type '%(st)s' is not valid for %(rt)s.")
                        % {"st": schema_type, "rt": self.ruleset_type},
                    },
                )


class Validator(TimeStampedModel):
    """
    A pluggable validator 'type' and version.
    Examples:
      kind='json_schema', version='2020-12'
      kind='xml_schema', version='1.0'
      kind='energyplus', version='23.1'
    """

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["slug", "version"],
                name="uq_validator_slug_version",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "validation_type",
                    "slug",
                ],
            ),
        ]

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_(
            "A unique identifier for the validator, used in URLs.",
        ),  # e.g. "json-2020-12", "eplus-23-1"
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional longer description of the validator."),
    )

    name = models.CharField(
        max_length=120,
        null=False,
        blank=False,
    )  # display label

    validation_type = models.CharField(
        max_length=40,
        choices=ValidationType.choices,
        null=False,
        blank=False,
    )

    version = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text=_("Version label for this validator (e.g. '2020-12', '1.0')."),
    )

    default_ruleset = models.ForeignKey(
        Ruleset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    def __str__(self):
        return f"{self.validation_type} {self.slug} v{self.version}"

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
        constraints = [
            # ended_at cannot be before started_at (allow nulls)
            models.CheckConstraint(
                name="ck_run_times_valid",
                condition=Q(ended_at__isnull=True)
                | Q(started_at__isnull=True)
                | Q(ended_at__gte=models.F("started_at")),
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="validation_runs",
    )

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.PROTECT,
        related_name="runs",
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
        Submission,
        on_delete=models.CASCADE,
        related_name="runs",
    )

    status = models.CharField(
        max_length=16,
        choices=ValidationRunStatus.choices,
        default=ValidationRunStatus.PENDING,
    )

    started_at = models.DateTimeField(null=True, blank=True)

    ended_at = models.DateTimeField(null=True, blank=True)

    duration_ms = models.BigIntegerField(default=0)

    summary = models.JSONField(default=dict, blank=True)  # counts, pass/fail, etc.

    error = models.TextField(
        blank=True, default="", null=True
    )  # terminal error/trace if any

    resolved_config = models.JSONField(
        default=dict,
        blank=True,
    )  # effective per-run config snapshot

    def clean(self):
        super().clean()
        # Optional but helpful: ensure org consistency with submission
        if self.submission_id and self.org_id and self.submission.org_id != self.org_id:
            raise ValidationError({"org": _("Run org must match submission org.")})


class ValidationStepRun(TimeStampedModel):
    """
    Execution of a single WorkflowStep within a ValidationRun.
    """

    class Meta:
        indexes = [models.Index(fields=["validation_run", "status"])]
        constraints = [
            # Prefer UniqueConstraint to future-proof
            models.UniqueConstraint(
                fields=[
                    "validation_run",
                    "step_order",
                ],
                name="uq_step_run_run_order",
            ),
            # Prevent duplicate execution rows for same step in
            # same run (optional but recommended)
            models.UniqueConstraint(
                fields=[
                    "validation_run",
                    "workflow_step",
                ],
                name="uq_step_run_run_step",
            ),
            models.CheckConstraint(
                name="ck_step_run_times_valid",
                condition=Q(ended_at__isnull=True)
                | Q(started_at__isnull=True)
                | Q(ended_at__gte=models.F("started_at")),
            ),
        ]

    validation_run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="step_runs",
    )

    workflow_step = models.ForeignKey(
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
        default=dict,
        blank=True,
    )  # machine output (validator-specific)

    error = models.TextField(blank=True, default="")

    def clean(self):
        super().clean()

        if (
            self.workflow_step
            and self.validation_run
            and self.workflow_step.workflow_id != self.validation_run.workflow_id
        ):
            raise ValidationError(
                {
                    "workflow_step": _("Step must belong to the run's workflow."),
                },
            )

        if (
            self.workflow_step
            and self.step_order
            and self.workflow_step.order != self.step_order
        ):
            raise ValidationError({"step_order": _("Must equal WorkflowStep.order.")})


class ValidationFinding(TimeStampedModel):
    """
    Normalized issues produced by step runs for efficient filtering/pagination.
    """

    class Meta:
        indexes = [
            models.Index(fields=["step_run", "severity"]),
            models.Index(fields=["step_run", "code"]),
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
        max_length=64,
        blank=True,
        default="",
    )  # e.g. "json.schema.required"

    message = models.TextField()

    path = models.CharField(
        max_length=512,
        blank=True,
        default="",
    )  # JSON Pointer/XPath/etc.

    meta = models.JSONField(default=dict, blank=True)


def artifact_upload_to(instance, filename: str) -> str:
    f = (
        f"artifacts/org-{instance.org_id}/runs/{instance.run_id}/"
        f"{uuid.uuid4().hex}/{filename}"
    )
    return f


class Artifact(TimeStampedModel):
    """
    Files emitted during a run (logs, reports, transformed docs, E+ outputs).
    """

    class Meta:
        indexes = [models.Index(fields=["validation_run", "created"])]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )

    validation_run = models.ForeignKey(
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
