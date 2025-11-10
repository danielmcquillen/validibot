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
from simplevalidations.validations.constants import AssertionOperator
from simplevalidations.validations.constants import AssertionType
from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogRunStage
from simplevalidations.validations.constants import CatalogValueType
from simplevalidations.validations.constants import CustomValidatorType
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
    """
    Reusable rule bundle (JSON Schema, XML schema, custom logic, etc.).

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

    @property
    def validator(self):
        """
        Returns the validator linked via any workflow step that references this ruleset.
        Falls back to None when the ruleset has not been attached yet.
        """
        from simplevalidations.workflows.models import WorkflowStep  # noqa: PLC0415

        step = (
            WorkflowStep.objects.filter(ruleset=self)
            .select_related("validator")
            .first()
        )
        validator = getattr(step, "validator", None)
        return validator


class RulesetAssertion(TimeStampedModel):
    """
    Normalized assertion definition tied to a ruleset.

    Assertions fall into two buckets:
    - BASIC assertions use a structured operator + payload.
    - CEL_EXPRESSION assertions store raw CEL plus optional guards.
    """

    ruleset = models.ForeignKey(
        Ruleset,
        on_delete=models.CASCADE,
        related_name="assertions",
    )

    order = models.PositiveIntegerField(default=0)

    assertion_type = models.CharField(
        max_length=32,
        choices=AssertionType.choices,
        default=AssertionType.BASIC,
    )

    operator = models.CharField(
        max_length=32,
        choices=AssertionOperator.choices,
        default=AssertionOperator.LE,
        help_text=_("Structured operator used for BASIC assertions."),
    )

    target_catalog = models.ForeignKey(
        "validations.ValidatorCatalogEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ruleset_assertions",
        help_text=_("Reference to a catalog entry when targeting a known signal."),
    )
    target_field = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "Custom JSON-style path for validators that allow free-form targets.",
        ),
    )

    severity = models.CharField(
        max_length=16,
        choices=Severity.choices,
        default=Severity.ERROR,
    )

    when_expression = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional CEL expression gating when the assertion evaluates."),
    )

    rhs = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Operator payload (values, range bounds, regex pattern, etc.)."),
    )

    options = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Operator options (inclusive bounds, tolerance metadata, case folding, etc.).",
        ),
    )

    message_template = models.TextField(
        blank=True,
        default="",
        help_text=_("Message rendered when the assertion fails."),
    )

    cel_cache = models.TextField(
        blank=True,
        default="",
        help_text=_("Normalized CEL preview generated from the stored payload."),
    )

    spec_version = models.PositiveIntegerField(
        default=1,
        help_text=_("Schema version for `rhs` and `options` payloads."),
    )

    class Meta:
        ordering = ["order", "pk"]
        constraints = [
            models.CheckConstraint(
                name="ck_ruleset_assertion_target_oneof",
                check=(
                    Q(target_catalog__isnull=False, target_field="")
                    | (Q(target_catalog__isnull=True) & ~Q(target_field=""))
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["ruleset", "order"]),
            models.Index(fields=["operator"]),
        ]

    def __str__(self):
        target = self.target_catalog.slug if self.target_catalog_id else self.target_field
        return f"{self.ruleset_id}:{self.operator}:{target or '?'}"

    @property
    def resolved_run_stage(self) -> CatalogRunStage:
        if self.target_catalog_id and self.target_catalog:
            return CatalogRunStage(self.target_catalog.run_stage)
        return CatalogRunStage.OUTPUT

    def clean(self):
        super().clean()
        catalog_set = bool(self.target_catalog_id)
        field = (self.target_field or "").strip()
        if catalog_set == bool(field):
            raise ValidationError(
                {
                    "target_field": _(
                        "Provide either a catalog target or a custom path (but not both).",
                    ),
                },
            )

    @property
    def target_display(self) -> str:
        if self.target_catalog_id and self.target_catalog:
            label = self.target_catalog.label or self.target_catalog.slug
            return f"{label} ({self.target_catalog.slug})"
        return self.target_field

    @property
    def condition_display(self) -> str:
        if self.assertion_type == AssertionType.CEL_EXPRESSION:
            return (self.rhs or {}).get("expr", "")
        formatter = self._format_literal
        rhs = self.rhs or {}
        options = self.options or {}
        op = AssertionOperator(self.operator)
        if op == AssertionOperator.LE:
            return _("≤ %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.LT:
            return _("< %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.GE:
            return _("≥ %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.GT:
            return _("> %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.EQ:
            return _("Equals %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.NE:
            return _("Not equals %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.BETWEEN:
            bounds = _(
                "%(min)s %(min_cmp)s target %(max_cmp)s %(max)s",
            ) % {
                "min": formatter(rhs.get("min")),
                "max": formatter(rhs.get("max")),
                "min_cmp": ">=" if options.get("include_min", True) else ">",
                "max_cmp": "<=" if options.get("include_max", True) else "<",
            }
            return bounds
        if op in {AssertionOperator.IN, AssertionOperator.NOT_IN}:
            values = ", ".join(formatter(v) for v in rhs.get("values", []))
            return (
                _("One of %(values)s") if op == AssertionOperator.IN else _("Not in %(values)s")
            ) % {"values": values}
        if op == AssertionOperator.MATCHES:
            return _("Matches %(pattern)s") % {"pattern": formatter(rhs.get("pattern"))}
        if op in {
            AssertionOperator.CONTAINS,
            AssertionOperator.NOT_CONTAINS,
            AssertionOperator.STARTS_WITH,
            AssertionOperator.ENDS_WITH,
        }:
            verb = self.get_operator_display()
            return _("%(verb)s %(value)s") % {
                "verb": verb,
                "value": formatter(rhs.get("value")),
            }
        if op in {AssertionOperator.IS_NULL, AssertionOperator.NOT_NULL}:
            return self.get_operator_display()
        if op == AssertionOperator.APPROX_EQ:
            tol = rhs.get("tolerance")
            return _("≈ %(value)s ± %(tol)s") % {
                "value": formatter(rhs.get("value")),
                "tol": formatter(tol),
            }
        return self.get_operator_display()

    def _format_literal(self, value) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return _("true") if value else _("false")
        return str(value)


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

    org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="validators",
        help_text=_(
            "Owning organization for custom validators (null for system validators)."
        ),
    )

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

    is_system = models.BooleanField(
        default=True,
        help_text=_("True when the validator ships with the platform."),
    )

    allow_custom_assertion_targets = models.BooleanField(
        default=False,
        help_text=_(
            "Allow authors to enter assertion targets not present in the catalog.",
        ),
    )

    @property
    def display_icon(self) -> str:
        bi_icon_class = {
            ValidationType.JSON_SCHEMA: "bi-filetype-json",
            ValidationType.XML_SCHEMA: "bi-filetype-xml",
            ValidationType.ENERGYPLUS: "bi-lightning-charge-fill",
            ValidationType.AI_ASSIST: "bi-robot",
        }.get(self.validation_type, "bi-journal-bookmark")  # default icon
        return bi_icon_class

    def __str__(self):
        prefix = f"{self.validation_type}"
        if self.org_id:
            prefix = f"{self.org.name} · {self.validation_type}"
        return f"{prefix} {self.slug} v{self.version}".strip()

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(f"{self.name}")
            if self.org_id:
                base_slug = slugify(f"{self.org_id}-{self.name}")
            self.slug = base_slug
        if self.org_id:
            self.is_system = False
        super().save(*args, **kwargs)

    @property
    def is_custom(self) -> bool:
        return bool(self.org_id and not self.is_system)

    def catalog_entries_by_type(self) -> dict[str, list["ValidatorCatalogEntry"]]:
        grouped: dict[str, list["ValidatorCatalogEntry"]] = {
            CatalogEntryType.SIGNAL: [],
            CatalogEntryType.DERIVATION: [],
        }
        for entry in self.catalog_entries.all().order_by("order", "slug"):
            grouped.setdefault(entry.entry_type, []).append(entry)
        return grouped

    def catalog_entries_by_stage(self) -> dict[str, list["ValidatorCatalogEntry"]]:
        grouped: dict[str, list["ValidatorCatalogEntry"]] = {
            CatalogRunStage.INPUT: [],
            CatalogRunStage.OUTPUT: [],
        }
        qs = (
            self.catalog_entries.filter(entry_type=CatalogEntryType.SIGNAL)
            .order_by("run_stage", "order", "slug")
        )
        for entry in qs:
            grouped.setdefault(entry.run_stage, []).append(entry)
        return grouped

    def has_signal_stage(self, stage: CatalogRunStage) -> bool:
        return self.catalog_entries.filter(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=stage,
        ).exists()

    def has_signal_stages(self) -> bool:
        return self.has_signal_stage(CatalogRunStage.INPUT) and self.has_signal_stage(
            CatalogRunStage.OUTPUT,
        )

    def get_catalog_entries(
        self,
        *,
        entry_type: CatalogEntryType | None = None,
    ) -> models.QuerySet["ValidatorCatalogEntry"]:
        qs = self.catalog_entries.all()
        if entry_type:
            qs = qs.filter(entry_type=entry_type)
        return qs


class ValidatorCatalogEntry(TimeStampedModel):
    """
    Catalog metadata describing signals, derivations, and other reusable items
    available to rulesets referencing a validator.
    """

    validator = models.ForeignKey(
        Validator,
        on_delete=models.CASCADE,
        related_name="catalog_entries",
    )
    entry_type = models.CharField(
        max_length=32,
        choices=CatalogEntryType.choices,
    )
    run_stage = models.CharField(
        max_length=16,
        choices=CatalogRunStage.choices,
        default=CatalogRunStage.INPUT,
        help_text=_("Phase of the validator run when this entry is available."),
    )
    slug = models.SlugField(
        max_length=255,
        help_text=_("Unique identifier for this catalog entry within the validator."),
    )
    label = models.CharField(
        max_length=255,
        help_text=_("Human-friendly label shown in editors."),
    )
    data_type = models.CharField(
        max_length=32,
        choices=CatalogValueType.choices,
        default=CatalogValueType.NUMBER,
    )
    description = models.TextField(
        blank=True,
        default="",
    )
    binding_config = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Provider-specific binding metadata."),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Additional UI metadata (example units, tags, etc.)."),
    )
    is_required = models.BooleanField(
        default=False,
        help_text=_("Whether this entry must be present for every ruleset."),
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["validator", "entry_type", "slug"],
                name="uq_validator_catalog_entry",
            ),
        ]
        ordering = [
            "order",
            "slug",
        ]

    def __str__(self):
        return f"{self.validator.slug}:{self.slug}"


class CustomValidator(TimeStampedModel):
    """
    Author-defined validator built on top of a base validation type.
    """

    validator = models.OneToOneField(
        Validator,
        on_delete=models.CASCADE,
        related_name="custom_validator",
    )
    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="custom_validator_configs",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="custom_validators",
    )
    custom_type = models.CharField(
        max_length=32,
        choices=CustomValidatorType.choices,
    )
    base_validation_type = models.CharField(
        max_length=40,
        choices=ValidationType.choices,
    )
    notes = models.TextField(
        blank=True,
        default="",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org", "custom_type", "validator"],
                name="uq_custom_validator_org_type_validator",
            ),
        ]

    def clean(self):
        super().clean()
        if self.validator.validation_type != self.base_validation_type:
            raise ValidationError(
                {
                    "base_validation_type": _(
                        "Base validation type must match the linked Validator validation_type.",
                    ),
                },
            )
        if self.validator.org_id and self.validator.org_id != self.org_id:
            raise ValidationError(
                {
                    "validator": _(
                        "Validator already belongs to a different organization.",
                    ),
                },
            )

    def save(self, *args, **kwargs):
        # Ensure the linked validator points at this org and is marked custom.
        self.validator.org = self.org
        self.validator.is_system = False
        if not self.validator.slug:
            self.validator.slug = slugify(f"{self.org_id}-{self.validator.name}")
        self.validator.save()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.org} · {self.custom_type}"


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
