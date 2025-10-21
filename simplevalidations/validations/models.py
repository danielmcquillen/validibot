from __future__ import annotations

import contextlib
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel
from slugify import slugify

from simplevalidations.projects.models import Project
from simplevalidations.submissions.models import Submission
from simplevalidations.users.models import Organization
from simplevalidations.users.models import User
from simplevalidations.validations.constants import JSONSchemaVersion
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import StepStatus
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.constants import XMLSchemaType
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowStep


class Ruleset(TimeStampedModel):
    """Reusable rule bundle (JSON Schema, XML schema, custom logic, etc.).

    Rules can be stored inline via ``rules_text`` or uploaded as ``rules_file``.
    Only one storage mechanism should be used at a time; helper ``rules``
    returns the effective rule definition as text.
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

    rules_file = models.FileField(
        upload_to="rulesets/",
        blank=True,
        help_text=_(
            "Optional uploaded file containing the ruleset definition. "
            "Leave empty when pasting rules directly.",
        ),
    )

    rules_text = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Inline ruleset definition (for example, JSON Schema or XML schema text). "
            "Use this when you prefer to paste or store the "
            "rules without uploading a file.",
        ),
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Additional metadata about the ruleset (non-rule data only)."),
    )

    def clean(self):
        super().clean()

        has_file = bool(self.rules_file and getattr(self.rules_file, "name", None))
        has_text = bool((self.rules_text or "").strip())
        if has_file and has_text:
            raise ValidationError(
                {
                    "rules_file": _("Provide rules either as text or file, not both."),
                    "rules_text": _("Provide rules either as text or file, not both."),
                },
            )

        # Validate XML schema_type when this ruleset is for XML
        if self.ruleset_type == RulesetType.XML_SCHEMA:
            meta = dict(self.metadata or {})
            schema_type_raw = str(meta.get("schema_type") or "").strip()
            if not schema_type_raw:
                raise ValidationError(
                    {
                        "metadata": _(
                            "XML schema rulesets must define metadata['schema_type'].",
                        ),
                    },
                )
            schema_type = schema_type_raw.upper()
            if schema_type not in set(XMLSchemaType.values):
                raise ValidationError(
                    {
                        "metadata": _("Schema type '%(st)s' is not valid for %(rt)s.")
                        % {"st": schema_type_raw, "rt": self.ruleset_type},
                    },
                )
            meta["schema_type"] = schema_type
            self.metadata = meta

        if self.ruleset_type == RulesetType.JSON_SCHEMA:
            meta = dict(self.metadata or {})
            schema_type_raw = str(meta.get("schema_type") or "").strip()
            if not schema_type_raw:
                raise ValidationError(
                    {
                        "metadata": _(
                            "JSON schema rulesets must define metadata['schema_type'].",
                        ),
                    },
                )
            schema_type_value = schema_type_raw
            if schema_type_value not in set(JSONSchemaVersion.values):
                candidate = schema_type_raw.upper()
                if candidate in JSONSchemaVersion.__members__:
                    schema_type_value = JSONSchemaVersion[candidate].value
                else:
                    raise ValidationError(
                        {
                            "metadata": _(
                                "Schema type '%(st)s' is not valid for %(rt)s.",
                            )
                            % {"st": schema_type_raw, "rt": self.ruleset_type},
                        },
                    )
            meta["schema_type"] = schema_type_value
            self.metadata = meta

        if self.ruleset_type in {RulesetType.JSON_SCHEMA, RulesetType.XML_SCHEMA}:
            if not has_file and not has_text:
                raise ValidationError(
                    {
                        "rules_text": _(
                            "Schema rulesets must include either inline "
                            "rules text or an uploaded file.",
                        ),
                    },
                )

    @property
    def rules(self) -> str:
        """
        Return the stored ruleset definition as text.

        Prefers inline ``rules_text`` and falls back to reading the uploaded file.
        """
        text = (self.rules_text or "").strip()
        if text:
            return text
        if self.rules_file:
            try:
                self.rules_file.open("rb")
                raw = self.rules_file.read()
            finally:
                with contextlib.suppress(Exception):
                    self.rules_file.close()
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace")
            return str(raw or "")
        return ""


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
        ordering = ["order", "name"]

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

    order = models.PositiveIntegerField(
        default=0,
        help_text=_("Relative ordering for display purposes."),
    )

    @property
    def display_icon(self) -> str:
        bi_icon_class = {
            ValidationType.JSON_SCHEMA: "bi-filetype-json",
            ValidationType.XML_SCHEMA: "bi-filetype-xml",
            ValidationType.ENERGYPLUS: "bi-lightning-charge-fill",
            ValidationType.AI_ASSIST: "bi-robot",
        }.get(self.validation_type, "bi-puzzle")  # default icon
        return bi_icon_class

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
        blank=True,
        default="",
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

    @property
    def status_pill_class(self) -> str:
        return {
            ValidationRunStatus.PENDING: "bg-secondary",
            ValidationRunStatus.RUNNING: "bg-primary",
            ValidationRunStatus.SUCCEEDED: "bg-success",
            ValidationRunStatus.FAILED: "bg-danger",
            ValidationRunStatus.CANCELED: "bg-warning text-dark",
        }.get(self.status, "bg-secondary")


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
            models.Index(fields=["validation_step_run", "severity"]),
            models.Index(fields=["validation_step_run", "code"]),
        ]

    validation_run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="findings",
    )

    validation_step_run = models.ForeignKey(
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
