from __future__ import annotations

import contextlib
import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models import Value
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel
from slugify import slugify

from validibot.core.models import CallbackReceiptStatus
from validibot.projects.models import Project
from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import data_format_allowed_file_types
from validibot.submissions.models import Submission
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ComputeTier
from validibot.validations.constants import CustomValidatorType
from validibot.validations.constants import FMUProbeStatus
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.constants import ValidatorWeight
from validibot.validations.constants import XMLSchemaType
from validibot.validations.constants import get_resource_types_for_validator
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep


def get_allowed_extensions_for_workflow(workflow) -> set[str]:
    """Collect allowed file extensions for all validators in a workflow.

    Returns a set of lowercase extensions (without leading dots) that are
    allowed for file uploads to this workflow. Reads from the config
    registry, falling back to empty for unknown validator types.
    """
    from validibot.validations.validators.base.config import get_config
    from validibot.workflows.models import WorkflowStep

    extensions: set[str] = set()
    steps = WorkflowStep.objects.filter(workflow=workflow).select_related("validator")
    for step in steps:
        validator = step.validator
        if not validator:
            continue
        cfg = get_config(validator.validation_type)
        if cfg:
            extensions.update(ext.lower() for ext in cfg.allowed_extensions)
    return extensions


def default_supported_file_types_for_validation(
    validation_type: str,
) -> list[str]:
    """Return the default supported file types for a validation type.

    Reads from the config registry. Falls back to deriving from data
    formats, or ``[JSON]`` if no config is registered.
    """
    from validibot.validations.validators.base.config import get_config

    cfg = get_config(validation_type)
    if cfg and cfg.supported_file_types:
        return list(cfg.supported_file_types)
    # Fallback: derive from data formats
    derived_formats = default_supported_data_formats_for_validation(validation_type)
    derived = supported_file_types_for_data_formats(derived_formats)
    return derived or [SubmissionFileType.JSON]


def default_supported_data_formats_for_validation(validation_type: str) -> list[str]:
    """Return the default supported data formats for a validation type.

    Reads from the config registry. Falls back to ``[JSON]`` if no config
    is registered.
    """
    from validibot.validations.validators.base.config import get_config

    cfg = get_config(validation_type)
    if cfg and cfg.supported_data_formats:
        return list(cfg.supported_data_formats)
    return [SubmissionDataFormat.JSON]


def supported_file_types_for_data_formats(data_formats: list[str]) -> list[str]:
    """
    Expand data formats to the submission file types that can carry them.
    """
    collected: list[str] = []
    for fmt in data_formats or []:
        for ft in data_format_allowed_file_types(fmt):
            if ft not in collected:
                collected.append(ft)
    return collected


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
        from validibot.workflows.models import WorkflowStep

        step = (
            WorkflowStep.objects.filter(ruleset=self)
            .select_related("validator")
            .first()
        )
        validator = getattr(step, "validator", None)
        return validator


class RulesetAssertion(TimeStampedModel):
    """
    A single rule that the assertion evaluator checks against validation data.

    Each assertion belongs to a ``Ruleset``, which in turn is attached either
    to a ``Validator`` (as its ``default_ruleset`` - always evaluated) or to
    an individual ``WorkflowStep`` (evaluated only when that step runs).
    At evaluation time the validator merges both sources, with default assertions
    ordered first, and evaluates them in a single pass via
    ``evaluate_assertions_for_stage()``.

    Assertion types:

    - **BASIC** - structured: an ``operator`` (e.g. le, between, matches)
      paired with ``rhs`` (operand payload) and ``options`` (tolerance,
      inclusive bounds, case folding, etc.).
    - **CEL_EXPRESSION** - free-form: the CEL source lives in ``rhs["expr"]``
      and is evaluated directly by the CEL evaluator.

    Targeting:

    Every assertion targets either a ``SignalDefinition`` (a known,
    typed signal published by the validator) *or* a free-form
    ``target_data_path`` - never both, enforced by the
    ``ck_ruleset_assertion_target_oneof`` check constraint.  The target
    also determines the ``resolved_run_stage`` (input vs output) that
    controls when the assertion fires.

    Messaging:

    ``message_template`` is rendered when the assertion *fails*;
    ``success_message`` when it *passes*.  Both support template variables
    from the evaluation context.
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

    target_signal_definition = models.ForeignKey(
        "validations.SignalDefinition",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ruleset_assertions",
        help_text=_("Reference to a signal definition when targeting a known signal."),
    )

    target_data_path = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "Custom JSON-style path for validators that allow free-form "
            "targets. Supports dot notation, [index], and filter "
            "expressions (e.g., items[?@.name=='x'].value).",
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
            "Operator options (inclusive bounds, tolerance "
            "metadata, case folding, etc.).",
        ),
    )

    message_template = models.TextField(
        blank=True,
        default="",
        help_text=_("Message rendered when the assertion fails."),
    )

    success_message = models.TextField(
        blank=True,
        default="",
        help_text=_("Message rendered when the assertion passes."),
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
                condition=(
                    Q(
                        target_signal_definition__isnull=False,
                        target_data_path="",
                    )
                    | Q(
                        target_signal_definition__isnull=True,
                    )
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["ruleset", "order"]),
            models.Index(fields=["operator"]),
        ]

    def __str__(self):
        if self.target_signal_definition_id and self.target_signal_definition:
            target = self.target_signal_definition.contract_key
        else:
            target = self.target_data_path
        return f"{self.ruleset_id}:{self.operator}:{target or '?'}"

    @property
    def resolved_run_stage(self) -> CatalogRunStage:
        if self.target_signal_definition_id and self.target_signal_definition:
            return CatalogRunStage(self.target_signal_definition.direction)
        path = (self.target_data_path or "").strip()
        if path.startswith(("s.", "signal.", "p.", "payload.")):
            return CatalogRunStage.INPUT
        return CatalogRunStage.OUTPUT

    def clean(self):
        super().clean()
        signal_set = bool(self.target_signal_definition_id)
        path_set = bool((self.target_data_path or "").strip())
        if signal_set and path_set:
            raise ValidationError(
                {
                    "target_data_path": _(
                        "Provide either a signal target or a custom path"
                        " (but not both)."
                    )
                },
            )

    @property
    def target_display(self) -> str:
        if self.target_signal_definition_id and self.target_signal_definition:
            sig = self.target_signal_definition
            prefix = "o." if sig.direction == SignalDirection.OUTPUT else "s."
            return f"{sig.label or sig.contract_key} ({prefix}{sig.contract_key})"
        return self.target_data_path

    @property
    def condition_display(self) -> str:
        if self.assertion_type == AssertionType.CEL_EXPRESSION:
            # CEL expression is already shown via target_display (stored in
            # target_data_path). Returning it here would duplicate it on the card.
            return ""
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
                _("One of %(values)s")
                if op == AssertionOperator.IN
                else _("Not in %(values)s")
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


def _default_validator_file_types() -> list[str]:
    return [SubmissionFileType.JSON]


def _default_validator_data_formats() -> list[str]:
    return [SubmissionDataFormat.JSON]


def _fmu_upload_path(instance: FMUModel, filename: str) -> str:
    org_segment = instance.org_id or "system"
    return f"fmu/{org_segment}/{uuid.uuid4()}-{filename}"


class FMUModel(TimeStampedModel):
    """
    Stored FMU artifact plus parsed metadata used by FMU validators.

    The FMU never executes inside Django; we store it for Modal runners to
    download and for offline inspection/probe runs. Each FMU also records a
    checksum and Modal Volume path so the Modal runtime can reuse a cached
    copy keyed by checksum.
    """

    class FMUKind(models.TextChoices):
        MODEL_EXCHANGE = "ModelExchange", _("Model Exchange")
        CO_SIMULATION = "CoSimulation", _("Co-Simulation")

    class FMUVersion(models.TextChoices):
        V2_0 = "2.0", _("FMI 2.0")
        V3_0 = "3.0", _("FMI 3.0")

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="fmu_models",
        null=True,
        blank=True,
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        related_name="fmu_models",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    file = models.FileField(upload_to=_fmu_upload_path)
    checksum = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_("SHA256 checksum used to reference cached FMUs in storage."),
    )
    gcs_uri = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=_(
            "Cloud storage URI to the canonical FMU object "
            "(e.g., gs://bucket/fmus/<checksum>.fmu for GCS)."
        ),
    )
    fmu_version = models.CharField(
        max_length=8,
        choices=FMUVersion.choices,
        default=FMUVersion.V2_0,
    )
    kind = models.CharField(
        max_length=32,
        choices=FMUKind.choices,
        default=FMUKind.CO_SIMULATION,
    )
    is_approved = models.BooleanField(default=False)
    size_bytes = models.BigIntegerField(default=0)
    introspection_metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.name} ({self.fmu_version}, {self.kind})"


class FMUVariable(TimeStampedModel):
    """
    Parsed variable metadata from modelDescription.xml attached to an FMUModel.

    These rows mirror ScalarVariable definitions from modelDescription.xml.
    """

    fmu_model = models.ForeignKey(
        FMUModel,
        on_delete=models.CASCADE,
        related_name="variables",
    )
    name = models.CharField(max_length=255)
    causality = models.CharField(max_length=64)
    variability = models.CharField(max_length=64, blank=True, default="")
    value_reference = models.BigIntegerField(default=0)
    value_type = models.CharField(max_length=64)
    unit = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "fmu_model",
                    "name",
                ],
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.name} ({self.causality})"


class FMUProbeResult(TimeStampedModel):
    """
    Tracks the latest probe state for an FMU prior to approval.

    Probe runs validate that an FMU can be opened safely and collect a clean
    variable snapshot for catalog seeding.
    """

    fmu_model = models.OneToOneField(
        FMUModel,
        on_delete=models.CASCADE,
        related_name="probe_result",
    )
    status = models.CharField(
        max_length=16,
        choices=FMUProbeStatus.choices,
        default=FMUProbeStatus.PENDING,
    )
    last_error = models.TextField(blank=True, default="")
    details = models.JSONField(default=dict, blank=True)

    def mark_failed(self, message: str, details: dict | None = None) -> None:
        self.status = FMUProbeStatus.FAILED
        self.last_error = message
        if details:
            self.details = details
        self.save(
            update_fields=[
                "status",
                "last_error",
                "details",
                "modified",
            ],
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
        ordering = ["order", "name"]

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_(
            "A unique identifier for the validator, used in URLs.",
        ),  # e.g. "json-2020-12", "eplus-23-1"
    )

    short_description = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "A brief summary of the validator's purpose. This description "
            "appears in lists and cards."
        ),
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
            "Owning organization for custom validators (null for system validators).",
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

    processor_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text=_(
            "The name of the process that generates output signals from input signals.",
        ),
    )

    has_processor = models.BooleanField(
        default=False,
        help_text=_(
            "True when the validator includes an intermediate "
            "processor that produces output signals."
        ),
    )
    supports_assertions = models.BooleanField(
        default=False,
        help_text=_(
            "True when this validator supports step-level assertions "
            "(Basic and CEL). Schema-only validators (JSON Schema, "
            "XML Schema) do not support assertions."
        ),
    )
    fmu_model = models.ForeignKey(
        "validations.FMUModel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validators",
        help_text=_(
            "FMU artifact backing this validator (only used for FMU validators).",
        ),
    )

    order = models.PositiveIntegerField(
        default=0,
        help_text=_("Relative ordering for display purposes."),
    )

    is_system = models.BooleanField(
        default=True,
        help_text=_("True when the validator ships with the platform."),
    )

    is_enabled = models.BooleanField(
        default=True,
        help_text=_(
            "Disabled validators are hidden from users and cannot be "
            "added to workflows. Toggle via the admin panel."
        ),
    )

    release_state = models.CharField(
        max_length=16,
        choices=ValidatorReleaseState.choices,
        default=ValidatorReleaseState.PUBLISHED,
        help_text=_(
            "Release state for system validators. DRAFT hides the validator, "
            "COMING_SOON shows it disabled, PUBLISHED makes it fully available."
        ),
    )

    allow_custom_assertion_targets = models.BooleanField(
        default=False,
        help_text=_(
            "Allow assertions against data paths not declared as signals.",
        ),
    )

    # Custom validators should only select a single data format from the allowed set
    CUSTOM_VALIDATOR_ALLOWED_DATA_FORMATS = {
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.YAML,
    }

    supported_data_formats = ArrayField(
        base_field=models.CharField(
            max_length=32,
            choices=SubmissionDataFormat.choices,
        ),
        default=_default_validator_data_formats,
        help_text=_(
            "Data formats this validator can parse (e.g., JSON, EnergyPlus IDF).",
        ),
    )

    supported_file_types = ArrayField(
        base_field=models.CharField(
            max_length=32,
            choices=SubmissionFileType.choices,
        ),
        default=_default_validator_file_types,
        help_text=_(
            "Logical file types this validator can process (JSON, XML, text, etc.).",
        ),
    )

    # Compute metering fields — used by the cloud billing system to classify
    # validators and calculate credit consumption. In the community edition
    # these are informational only.
    compute_tier = models.CharField(
        max_length=10,
        choices=ComputeTier.choices,
        default=ComputeTier.LOW,
        help_text=_(
            "Compute intensity classification. LOW = metered by launch count. "
            "HIGH = metered by credit consumption."
        ),
    )
    compute_weight = models.PositiveSmallIntegerField(
        default=ValidatorWeight.NORMAL,
        choices=ValidatorWeight.choices,
        help_text=_(
            "Credit multiplier for HIGH-compute validators. "
            "Higher weight = more credits consumed per minute of runtime."
        ),
    )

    @property
    def card_image_name(self) -> str:
        """Return the card image filename for the validator library UI.

        Reads from the config registry. Falls back to the default card
        image for validators without a registered config.
        """
        from validibot.validations.validators.base.config import get_config

        cfg = get_config(self.validation_type)
        if cfg:
            return cfg.card_image
        return "default_card_img_small.png"

    @property
    def display_icon(self) -> str:
        """Return the Bootstrap Icons CSS class for this validator type.

        Reads from the config registry. Falls back to a generic icon
        for validators without a registered config.
        """
        from validibot.validations.validators.base.config import get_config

        cfg = get_config(self.validation_type)
        if cfg:
            return cfg.icon
        return "bi-journal-bookmark"

    @property
    def is_published(self) -> bool:
        """Return True if validator is published and fully available."""
        return self.release_state == ValidatorReleaseState.PUBLISHED

    @property
    def is_coming_soon(self) -> bool:
        """Return True if validator is marked as coming soon."""
        return self.release_state == ValidatorReleaseState.COMING_SOON

    @property
    def is_draft(self) -> bool:
        """Return True if validator is in draft state (hidden)."""
        return self.release_state == ValidatorReleaseState.DRAFT

    @property
    def supports_resource_files(self) -> bool:
        """Return True if this validator type accepts resource files."""
        return bool(get_resource_types_for_validator(self.validation_type))

    def __str__(self):
        prefix = f"{self.validation_type}"
        if self.org_id:
            prefix = f"{self.org.name} · {self.validation_type}"
        return f"{prefix} {self.slug} v{self.version}".strip()

    def clean(self):
        super().clean()
        data_formats = [value for value in (self.supported_data_formats or []) if value]
        file_types_hint = [
            value for value in (self.supported_file_types or []) if value
        ]
        placeholder_formats = _default_validator_data_formats()
        placeholder_file_types = _default_validator_file_types()
        expected_formats = default_supported_data_formats_for_validation(
            self.validation_type,
        )
        # If formats/file types are still on the placeholder defaults, apply the
        # validation-type defaults so compatibility checks stay accurate.
        if not data_formats or (
            data_formats == placeholder_formats
            and expected_formats != placeholder_formats
            and (not file_types_hint or file_types_hint == placeholder_file_types)
        ):
            data_formats = expected_formats
        normalized_formats: list[str] = []
        for value in data_formats:
            if value not in SubmissionDataFormat.values:
                raise ValidationError(
                    {
                        "supported_data_formats": _(
                            "'%(value)s' is not a supported submission data format.",
                        )
                        % {"value": value},
                    },
                )
            if value not in normalized_formats:
                normalized_formats.append(value)
        if self.validation_type == ValidationType.CUSTOM_VALIDATOR:
            invalid = [
                value
                for value in normalized_formats
                if value not in self.CUSTOM_VALIDATOR_ALLOWED_DATA_FORMATS
            ]
            if invalid:
                raise ValidationError(
                    {
                        "supported_data_formats": _(
                            "Custom validators support only JSON or YAML."
                        ),
                    },
                )
            if len(normalized_formats) != 1:
                raise ValidationError(
                    {
                        "supported_data_formats": _(
                            "Select exactly one data format for a custom validator."
                        ),
                    },
                )
        self.supported_data_formats = normalized_formats

        derived_file_types = supported_file_types_for_data_formats(
            self.supported_data_formats,
        )
        file_types = [value for value in (self.supported_file_types or []) if value]
        if not file_types or file_types == _default_validator_file_types():
            file_types = default_supported_file_types_for_validation(
                self.validation_type,
            )
        normalized_files: list[str] = []
        for value in file_types:
            if value not in SubmissionFileType.values:
                raise ValidationError(
                    {
                        "supported_file_types": _(
                            "'%(value)s' is not a supported submission file type.",
                        )
                        % {"value": value},
                    },
                )
            if value not in normalized_files:
                normalized_files.append(value)
        for derived in derived_file_types:
            if derived not in normalized_files:
                normalized_files.append(derived)
        self.supported_file_types = normalized_files

        if self.validation_type == ValidationType.FMU:
            if not self.fmu_model_id:
                if not self.is_system:
                    raise ValidationError(
                        {
                            "fmu_model": _(
                                "Assign an FMU asset before saving an FMU validator.",
                            ),
                        },
                    )
        elif self.fmu_model_id:
            raise ValidationError(
                {
                    "fmu_model": _(
                        "FMU assets can only be attached to FMU validators.",
                    ),
                },
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.slug:
            base_slug = slugify(f"{self.name}")
            if self.org_id:
                base_slug = slugify(f"{self.org_id}-{self.name}")
            self.slug = base_slug
        if self.org_id:
            self.is_system = False
        super().save(*args, **kwargs)
        self.ensure_default_ruleset()

    def ensure_default_ruleset(self) -> Ruleset:
        """Ensure this validator has a default_ruleset, creating one if needed.

        The default ruleset holds validator-level assertions that run on every
        workflow step using this validator. It's auto-created on first save.

        Returns:
            The existing or newly created default Ruleset.
        """
        if self.default_ruleset_id:
            return self.default_ruleset

        # Map validation_type to RulesetType. They share the same values
        # except AI_ASSIST which falls back to BASIC.
        ruleset_type = self.validation_type
        if ruleset_type not in RulesetType.values:
            ruleset_type = RulesetType.BASIC

        ruleset = Ruleset.objects.create(
            name=f"{self.name} - Default Assertions",
            ruleset_type=ruleset_type,
            org=self.org,
        )
        # Use update() to avoid re-triggering save/full_clean
        Validator.objects.filter(pk=self.pk).update(default_ruleset=ruleset)
        self.default_ruleset = ruleset
        self.default_ruleset_id = ruleset.pk
        return ruleset

    @property
    def is_custom(self) -> bool:
        return bool(self.org_id and not self.is_system)

    def supports_file_type(self, file_type: str) -> bool:
        normalized = (file_type or "").lower()
        allowed = {value.lower() for value in (self.supported_file_types or [])}
        return normalized in allowed

    def supports_data_format(self, data_format: str) -> bool:
        normalized = (data_format or "").lower()
        allowed = {value.lower() for value in (self.supported_data_formats or [])}
        return normalized in allowed

    def supports_any_file_type(self, file_types: list[str]) -> bool:
        allowed = {value.lower() for value in (self.supported_file_types or [])}
        incoming = {value.lower() for value in file_types}
        return bool(allowed & incoming)

    def supported_file_type_labels(self) -> list[str]:
        labels: list[str] = []
        for value in self.supported_file_types or []:
            try:
                labels.append(str(SubmissionFileType(value).label))
            except Exception:
                labels.append(str(value))
        return labels

    def supported_data_format_labels(self) -> list[str]:
        labels: list[str] = []
        for value in self.supported_data_formats or []:
            try:
                labels.append(str(SubmissionDataFormat(value).label))
            except Exception:
                labels.append(str(value))
        return labels


# ── Unified Signal Model ────────────────────────────────────────────
#
# These four models implement the unified signal architecture described
# in ADR-2026-03-18. They provide a single relational model for signal
# metadata that supports contract definition, per-step binding, computed
# derivations, and runtime audit tracing.
# ─────────────────────────────────────────────────────────────────────


class SignalDefinition(TimeStampedModel):
    """The stable data contract for a named signal.

    A SignalDefinition declares that a validator or workflow step expects
    (input) or produces (output) a named data point with a specific type.
    It is the "what" — the contract — not the "where" (that is the binding).

    This model unifies signal metadata that was previously scattered across
    three legacy storage formats (ValidatorCatalogEntry, FMU config
    JSON, template config JSON) into a single relational table. Every
    feature that touches
    signals — the signals UI, CEL context building, FMU envelope assembly,
    assertion evaluation — works against this one model.

    Key concepts:

    **contract_key vs native_name:** ``contract_key`` is the stable,
    slug-safe identifier used in CEL expressions, the API, and data path
    bindings (e.g., ``panel_area``). ``native_name`` preserves the
    provider's original name verbatim (e.g., an FMU's modelDescription.xml
    variable name ``Panel.Area_m2`` or an EnergyPlus template variable
    ``#{heating_setpoint}``). The contract_key is what Validibot uses;
    the native_name is what the provider uses. They may be identical for
    simple cases.

    **Ownership (XOR constraint):** Each signal is owned by exactly one of:
    - A ``Validator`` — shared signal definitions that apply to every step
      using that validator (library validators).
    - A ``WorkflowStep`` — per-step signal definitions for step-level FMU
      uploads, template scans, or author-customized signals.

    **source_kind:** Declares how the signal's value is obtained.
    ``PAYLOAD_PATH`` means the value comes from a known path in the
    submission payload or metadata (the author may or may not be able
    to change the path — see ``is_path_editable``). ``INTERNAL`` means
    the validator has its own mechanism for extracting or computing the
    value (e.g., EnergyPlus parsing simulation metrics, FMU reading
    output variables). This distinction is surfaced in the UI so
    workflow authors know which signals they can configure.

    **is_path_editable:** Controls whether the workflow author can edit
    the source data path for this signal's ``StepSignalBinding``. When
    ``False``, the source path field in the signal edit modal is
    disabled. Typically ``False`` for signals where the validator
    controls the extraction path internally (all EnergyPlus and THERM
    signals, FMU output signals).

    **provider_binding:** A JSON column holding validator-type-specific
    properties that the resolution layer needs but that are not part of the
    generic signal contract. For FMU signals this includes ``causality``,
    ``value_reference``, and ``variability``. For EnergyPlus template
    signals this includes ``variable_type``, ``min``, ``max``, and
    ``choices``. Typed access is provided by Pydantic accessor models.

    See ADR-2026-03-18 for the full design rationale.
    """

    contract_key = models.SlugField(
        max_length=255,
        help_text=(
            "Stable slug identifier used in CEL expressions, API responses, "
            "and data path bindings. Must be unique per owner and direction."
        ),
    )
    native_name = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "The provider's original name for this signal, preserved "
            "verbatim (e.g., an FMU variable name or template placeholder)."
        ),
    )
    label = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Human-readable display label for the signal.",
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Detailed description of what this signal represents.",
    )
    direction = models.CharField(
        max_length=10,
        choices=SignalDirection.choices,
        help_text="Whether this signal is consumed (input) or produced (output).",
    )
    data_type = models.CharField(
        max_length=20,
        choices=CatalogValueType.choices,
        default=CatalogValueType.NUMBER,
        help_text="The data type of the signal value.",
    )
    origin_kind = models.CharField(
        max_length=20,
        choices=SignalOriginKind.choices,
        help_text=(
            "How this signal definition was created: "
            "from a validator config declaration, "
            "an FMU probe, or a template scan."
        ),
    )
    source_kind = models.CharField(
        max_length=20,
        choices=SignalSourceKind.choices,
        default=SignalSourceKind.PAYLOAD_PATH,
        help_text=(
            "How the signal's value is obtained: from a known data path "
            "in the submission payload (PAYLOAD_PATH) or via the "
            "validator's own internal extraction mechanism (INTERNAL)."
        ),
    )
    is_path_editable = models.BooleanField(
        default=True,
        help_text=(
            "Whether the workflow author can edit the source data path "
            "for this signal's step binding. False for signals where "
            "the validator controls the extraction path internally."
        ),
    )
    validator = models.ForeignKey(
        "Validator",
        on_delete=models.CASCADE,
        related_name="signal_definitions",
        null=True,
        blank=True,
        help_text=(
            "The validator that owns this signal (for library validators). "
            "Mutually exclusive with workflow_step."
        ),
    )
    workflow_step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="signal_definitions",
        null=True,
        blank=True,
        help_text=(
            "The workflow step that owns this signal (for step-level FMUs, "
            "templates, or author-customized signals). "
            "Mutually exclusive with validator."
        ),
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Display ordering within the owner's signal list.",
    )
    is_hidden = models.BooleanField(
        default=False,
        help_text="If True, this signal is hidden from the default signals UI.",
    )
    unit = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Unit of measurement (e.g., 'kW', 'm2', 'degC').",
    )
    provider_binding = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Validator-type-specific properties needed by the resolution "
            "layer (e.g., FMU causality/value_reference, EnergyPlus "
            "variable_type/min/max). Typed access via Pydantic models."
        ),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary metadata for extensions and integrations.",
    )
    signal_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "Optional signal name for output promotion. When set on an "
            "output-direction SignalDefinition, the output value is "
            "promoted to the s (signal) namespace in CEL expressions "
            "as s.<signal_name>. Must be a valid CEL identifier."
        ),
    )

    class Meta:
        constraints = [
            # Exactly one of validator or workflow_step must be set.
            models.CheckConstraint(
                condition=(
                    Q(validator__isnull=False, workflow_step__isnull=True)
                    | Q(validator__isnull=True, workflow_step__isnull=False)
                ),
                name="ck_sigdef_one_owner",
            ),
            # Unique (validator, contract_key, direction) for validator-owned.
            models.UniqueConstraint(
                fields=["validator", "contract_key", "direction"],
                condition=Q(validator__isnull=False),
                name="uq_sigdef_validator_key_dir",
            ),
            # Unique (workflow_step, contract_key, direction) for step-owned.
            models.UniqueConstraint(
                fields=["workflow_step", "contract_key", "direction"],
                condition=Q(workflow_step__isnull=False),
                name="uq_sigdef_step_key_dir",
            ),
        ]
        ordering = ["order", "pk"]

    # ── Typed metadata accessors ─────────────────────────────

    @property
    def fmu_metadata(self):
        """Typed access to FMU-specific UI/presentation metadata."""
        from validibot.validations.signal_metadata.metadata import FMUSignalMetadata

        return FMUSignalMetadata(**(self.metadata or {}))

    @property
    def fmu_binding(self):
        """Typed access to FMU-specific provider binding."""
        from validibot.validations.signal_metadata.metadata import FMUProviderBinding

        return FMUProviderBinding(**(self.provider_binding or {}))

    @property
    def template_metadata(self):
        """Typed access to template-specific UI/presentation metadata."""
        from validibot.validations.signal_metadata.metadata import (
            TemplateSignalMetadata,
        )

        return TemplateSignalMetadata(**(self.metadata or {}))

    @property
    def energyplus_binding(self):
        """Typed access to EnergyPlus-specific provider binding."""
        from validibot.validations.signal_metadata.metadata import (
            EnergyPlusProviderBinding,
        )

        return EnergyPlusProviderBinding(**(self.provider_binding or {}))

    def __str__(self):
        owner = self.validator or self.workflow_step
        return f"{owner}:{self.contract_key} ({self.direction})"


class StepSignalBinding(TimeStampedModel):
    """Per-step wiring that maps a validator input to a data source.

    While ``SignalDefinition`` declares *what* data a step expects,
    ``StepSignalBinding`` declares *where* to find it. This is the
    per-step mapping layer that connects each validator input to a
    concrete location in the submission payload, submission metadata,
    a workflow signal, an upstream step's output, or a system value.

    This separation allows the same signal definition (e.g., ``panel_area``)
    to be wired differently in different workflow steps — one step might
    read it from ``building.envelope.panel_area`` in the submission JSON,
    while another reads it from a workflow signal or upstream step output.

    Key fields:

    - ``source_scope``: Where to look for the value (submission payload,
      submission metadata, workflow signal, upstream step output, or system).
    - ``source_data_path``: A dotted path expression (e.g.,
      ``weather.stations[0].solar_irradiance``) into the source scope.
    - ``default_value``: Fallback value when the source path resolves to
      nothing and the input is not required.
    - ``is_required``: If True, a missing value with no default raises a
      structured error before validator execution.

    See ADR-2026-03-18 for the full binding and resolution design.
    """

    workflow_step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="signal_bindings",
        help_text="The workflow step this binding belongs to.",
    )
    signal_definition = models.ForeignKey(
        SignalDefinition,
        on_delete=models.CASCADE,
        related_name="bindings",
        help_text="The validator input or output definition this binding wires up.",
    )
    source_scope = models.CharField(
        max_length=30,
        choices=BindingSourceScope.choices,
        default=BindingSourceScope.SUBMISSION_PAYLOAD,
        help_text=(
            "The data scope to resolve the input value from: submission "
            "payload, submission metadata, workflow signal, upstream step "
            "output, or system."
        ),
    )
    source_data_path = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Path expression into the source scope. Supports dot "
            "notation (e.g., 'building.panel_area'), [index] for "
            "arrays, and filter expressions for named elements "
            "(e.g., 'items[?@.name==\"x\"].value')."
        ),
    )
    default_value = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            "Fallback value used when the source path resolves to nothing "
            "and the input is not marked as required."
        ),
    )
    is_required = models.BooleanField(
        default=True,
        help_text=(
            "If True, a missing value with no default raises a structured "
            "error before validator execution begins."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_step", "signal_definition"],
                name="uq_binding_step_signal",
            ),
        ]

    def __str__(self):
        return (
            f"{self.workflow_step}:{self.signal_definition.contract_key} "
            f"← {self.source_scope}:{self.source_data_path}"
        )


class Derivation(TimeStampedModel):
    """A computed value defined by a CEL expression over signals.

    Derivations are named, typed values that are computed from input
    signals and other derivations using CEL (Common Expression Language)
    expressions. They represent a distinct concept from signals: signals
    are data points that flow in or out of a validator; derivations are
    intermediate computed values used in assertions and reporting.

    Like ``SignalDefinition``, each derivation is owned by exactly one of
    a ``Validator`` (shared across all steps using that validator) or a
    ``WorkflowStep`` (per-step customization). The same XOR ownership
    constraint applies.

    The ``expression`` field contains a CEL expression that can reference
    signal contract_keys and other derivation contract_keys by name.
    The ``data_type`` is limited to scalar types (number, string, boolean)
    since derivations produce single computed values, not complex objects
    or timeseries.

    See ADR-2026-03-18 section on derivations for the full design.
    """

    contract_key = models.SlugField(
        max_length=255,
        help_text=(
            "Stable slug identifier for this derivation, used in CEL "
            "expressions and API responses."
        ),
    )
    expression = models.TextField(
        help_text=(
            "CEL expression that computes this derivation's value. Can "
            "reference signal contract_keys and other derivation keys."
        ),
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Detailed description of what this derivation computes.",
    )
    label = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Human-readable display label for the derivation.",
    )
    data_type = models.CharField(
        max_length=20,
        choices=[
            (CatalogValueType.NUMBER, CatalogValueType.NUMBER.label),
            (CatalogValueType.STRING, CatalogValueType.STRING.label),
            (CatalogValueType.BOOLEAN, CatalogValueType.BOOLEAN.label),
        ],
        default=CatalogValueType.NUMBER,
        help_text=(
            "The data type of the computed value. Limited to scalar types "
            "(number, string, boolean)."
        ),
    )
    validator = models.ForeignKey(
        "Validator",
        on_delete=models.CASCADE,
        related_name="derivations",
        null=True,
        blank=True,
        help_text=(
            "The validator that owns this derivation (for library validators). "
            "Mutually exclusive with workflow_step."
        ),
    )
    workflow_step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="derivations",
        null=True,
        blank=True,
        help_text=(
            "The workflow step that owns this derivation. "
            "Mutually exclusive with validator."
        ),
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Display ordering within the owner's derivation list.",
    )
    is_hidden = models.BooleanField(
        default=False,
        help_text="If True, this derivation is hidden from the default UI.",
    )
    unit = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Unit of measurement for the computed value.",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary metadata for extensions and integrations.",
    )

    class Meta:
        constraints = [
            # Exactly one of validator or workflow_step must be set.
            models.CheckConstraint(
                condition=(
                    Q(validator__isnull=False, workflow_step__isnull=True)
                    | Q(validator__isnull=True, workflow_step__isnull=False)
                ),
                name="ck_derivation_one_owner",
            ),
            # Unique (validator, contract_key) for validator-owned.
            models.UniqueConstraint(
                fields=["validator", "contract_key"],
                condition=Q(validator__isnull=False),
                name="uq_derivation_validator_key",
            ),
            # Unique (workflow_step, contract_key) for step-owned.
            models.UniqueConstraint(
                fields=["workflow_step", "contract_key"],
                condition=Q(workflow_step__isnull=False),
                name="uq_derivation_step_key",
            ),
        ]
        ordering = ["order", "pk"]

    def __str__(self):
        owner = self.validator or self.workflow_step
        return f"{owner}:{self.contract_key}"


class ResolvedInputTrace(TimeStampedModel):
    """Runtime audit record of how an input signal was resolved.

    Each time the resolution engine processes a step run's input signals,
    it creates one ``ResolvedInputTrace`` per input signal. This provides
    a complete audit trail of:

    - Which signal was being resolved (``signal_definition`` FK and
      denormalized ``signal_contract_key``).
    - Which source scope and data path were used to find the value.
    - Whether resolution succeeded (``resolved``), fell back to a default
      (``used_default``), or failed (``error_message``).
    - A snapshot of the resolved value for debugging and reproducibility.

    The ``signal_contract_key`` is denormalized from the signal definition
    so that traces remain meaningful even if the signal definition is later
    deleted (the FK is SET_NULL). This supports long-term auditability
    without preventing schema evolution.

    The ``upstream_step_key`` is populated when ``source_scope_used`` is
    ``upstream_step``, recording which step's output was consulted.

    See ADR-2026-03-18 for the full resolution and audit design.
    """

    step_run = models.ForeignKey(
        "ValidationStepRun",
        on_delete=models.CASCADE,
        related_name="input_traces",
        help_text="The step run this trace belongs to.",
    )
    signal_definition = models.ForeignKey(
        SignalDefinition,
        on_delete=models.SET_NULL,
        null=True,
        help_text=(
            "The signal definition that was being resolved. SET_NULL on "
            "delete so traces survive schema evolution."
        ),
    )
    signal_contract_key = models.CharField(
        max_length=255,
        help_text=(
            "Denormalized contract_key from the signal definition, "
            "preserved for auditability if the definition is deleted."
        ),
    )
    source_scope_used = models.CharField(
        max_length=30,
        help_text=(
            "The source scope that was actually used to resolve the value "
            "(may differ from the binding's configured scope if fallback "
            "logic was applied)."
        ),
    )
    source_data_path_used = models.CharField(
        max_length=500,
        help_text=("The data path that was actually used to resolve the value."),
    )
    upstream_step_key = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "If source_scope_used is 'upstream_step', the key of the "
            "upstream step whose output was consulted."
        ),
    )
    resolved = models.BooleanField(
        help_text="Whether the resolution engine found a value for this signal.",
    )
    used_default = models.BooleanField(
        default=False,
        help_text=(
            "Whether the resolved value came from the binding's default_value "
            "rather than the source data path."
        ),
    )
    value_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text=(
            "Snapshot of the resolved value at resolution time, for "
            "debugging and reproducibility."
        ),
    )
    error_message = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Error message if resolution failed (e.g., required signal "
            "not found and no default configured)."
        ),
    )

    def __str__(self):
        status = "resolved" if self.resolved else "FAILED"
        return f"{self.signal_contract_key} → {status}"


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
                        "Base validation type must match the linked "
                        "Validator validation_type.",
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
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="runs",
        help_text=_(
            "Can be NULL if submission record was deleted. "
            "Code accessing this field must handle the None case."
        ),
    )

    status = models.CharField(
        max_length=16,
        choices=ValidationRunStatus.choices,
        default=ValidationRunStatus.PENDING,
    )

    short_description = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_("Optional short description of this validation run."),
    )

    started_at = models.DateTimeField(null=True, blank=True)

    ended_at = models.DateTimeField(null=True, blank=True)

    duration_ms = models.BigIntegerField(default=0)

    summary = models.JSONField(default=dict, blank=True)  # counts, pass/fail, etc.

    error = models.TextField(
        blank=True,
        default="",
    )  # terminal error/trace if any

    error_category = models.CharField(
        max_length=32,
        choices=ValidationRunErrorCategory.choices,
        blank=True,
        default="",
        help_text=_("Classification of why the run failed (TIMEOUT, OOM, etc.)."),
    )

    source = models.CharField(
        max_length=32,
        choices=ValidationRunSource.choices,
        default=ValidationRunSource.LAUNCH_PAGE,
        help_text=_("Where this run was initiated (web launch page, API, etc.)."),
    )

    # Output retention fields
    # ~---------------------------------------------------------------
    # These track when validator outputs (results, artifacts, findings) should
    # be purged. The retention policy is snapshotted from the workflow at
    # run creation time.

    output_retention_policy = models.CharField(
        max_length=32,
        choices=OutputRetention.choices,
        default=OutputRetention.STORE_30_DAYS,
        help_text=_("Snapshot of workflow's output retention policy at run time."),
    )

    output_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "When outputs should be purged (null = never expires or not yet computed)."
        ),
    )

    output_purged_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When outputs were purged (for audit trail)."),
    )

    output_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "SHA-256 hash of the canonical validation output record, computed "
            "at run completion. Covers the stable workflow context and the "
            "final run outcome."
        ),
    )

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

    @property
    def computed_duration_ms(self) -> int | None:
        if self.duration_ms:
            return int(self.duration_ms)
        if not self.started_at or not self.ended_at:
            return None
        delta = self.ended_at - self.started_at
        return max(int(delta.total_seconds() * 1000), 0)

    @property
    def current_step_run(self) -> ValidationStepRun | None:
        """
        Returns the currently active ValidationStepRun for this run.

        Active means status is PENDING or RUNNING. If multiple steps are active
        (which shouldn't happen), returns the first one by step_order.
        """
        from validibot.validations.constants import StepStatus

        return (
            self.step_runs.filter(
                status__in=[StepStatus.PENDING, StepStatus.RUNNING],
            )
            .order_by("step_order")
            .first()
        )

    @property
    def user_friendly_error(self) -> str:
        """
        Returns a human-friendly error message based on error_category.

        Falls back to the raw error text if no category is set, or empty
        string if there's no error at all.
        """
        from validibot.validations.constants import VALIDATION_RUN_ERROR_MESSAGES

        if self.error_category:
            return VALIDATION_RUN_ERROR_MESSAGES.get(
                self.error_category,
                self.error or "",
            )
        return self.error or ""


class ValidationRunSummary(TimeStampedModel):
    """
    Durable aggregate snapshot of a ValidationRun.

    Retains severity totals and per-step metadata even after findings are purged.
    """

    class Meta:
        ordering = ["-created"]

    run = models.OneToOneField(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="summary_record",
        primary_key=True,
    )

    status = models.CharField(
        max_length=16,
        choices=ValidationRunStatus.choices,
        default=ValidationRunStatus.PENDING,
    )

    completed_at = models.DateTimeField(null=True, blank=True)

    total_findings = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    info_count = models.PositiveIntegerField(default=0)

    assertion_failure_count = models.PositiveIntegerField(default=0)
    assertion_total_count = models.PositiveIntegerField(default=0)

    extras = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Optional metadata for reporting (for example exemplar messages)."),
    )

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"Summary for run {self.run_id} ({self.status})"


class ValidationStepRun(TimeStampedModel):
    """
    Execution of a single WorkflowStep within a ValidationRun.

    Results of the step run are stored as ValidationFindings linked to this model.

    """

    class Meta:
        indexes = [
            models.Index(fields=["validation_run", "status"]),
        ]
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

    def __str__(self) -> str:
        if self.workflow_step:
            name = (
                f"Results from Step {self.workflow_step.step_number} :"
                f"{self.workflow_step.name}"
            )
            return name
        return super().__str__()

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


class ValidationStepRunSummary(TimeStampedModel):
    """
    Lightweight per-step snapshot tied to ValidationRunSummary.
    """

    class Meta:
        ordering = ["step_order", "id"]
        indexes = [
            models.Index(fields=["summary", "status"]),
        ]

    summary = models.ForeignKey(
        ValidationRunSummary,
        on_delete=models.CASCADE,
        related_name="step_summaries",
    )

    step_run = models.OneToOneField(
        ValidationStepRun,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="step_summary",
    )

    step_name = models.CharField(max_length=255, blank=True, default="")
    step_order = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=16,
        choices=StepStatus.choices,
        default=StepStatus.PENDING,
    )

    error_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    info_count = models.PositiveIntegerField(default=0)


class ValidationFinding(TimeStampedModel):
    """
    Normalized issue emitted during a validation step.

    A ValidationFinding is the durable, queryable representation of a single
    validator message (error/warning/info) produced while executing a
    ValidationStepRun. Findings are stored separately from validator-specific
    `ValidationStepRun.output` so the UI/API can filter, sort, and paginate
    issues efficiently.

    Data model relationships:
    - `validation_step_run` is the primary parent for the finding.
    - `validation_run` is denormalized for performance (kept aligned by
      `_ensure_run_alignment`).
    - `ruleset_assertion` optionally links the finding back to a configured
      RulesetAssertion when the validator can attribute the issue to an assertion.

    Aggregates such as ValidationRunSummary and ValidationStepRunSummary are
    computed from this table (severity totals, per-step counts).

    The `path` field records a location within the submitted artifact (JSON
    Pointer, XPath, etc.). For JSON submissions we strip the synthetic `payload`
    prefix to match user-facing paths (see `_strip_payload_prefix`).
    """

    class Meta:
        indexes = [
            models.Index(fields=["validation_run", "severity"]),
            models.Index(fields=["validation_step_run", "severity"]),
            models.Index(fields=["validation_step_run", "code"]),
        ]
        ordering = [
            models.Case(
                models.When(severity=Severity.ERROR, then=Value(0)),
                models.When(severity=Severity.WARNING, then=Value(1)),
                default=Value(2),
                output_field=models.IntegerField(),
            ),
            "-created",
        ]

    # We keep a link to both the run and the step run for easier querying.
    # This is a bit of denormalization but improves performance.
    # (Theroetically we could get the run via step_run.validation_run.)
    # To mitigate any issues we define _ensure_run_alignment below and
    # call it in clean().

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

    ruleset_assertion = models.ForeignKey(
        "validations.RulesetAssertion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
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

    def _ensure_run_alignment(self) -> None:
        """
        Guarantee validation_run mirrors the parent run from validation_step_run.
        """

        if not self.validation_step_run_id:
            return

        step_run = self.validation_step_run
        if not step_run:
            return

        parent_run_id = step_run.validation_run_id
        if not parent_run_id:
            return

        if not self.validation_run_id:
            self.validation_run_id = parent_run_id
            return

        if self.validation_run_id != parent_run_id:
            raise ValidationError(
                {
                    "validation_run": _(
                        "Validation run must match the step run's parent run.",
                    ),
                },
            )

    def _strip_payload_prefix(self) -> None:
        """Remove the synthetic 'payload' prefix for JSON submissions."""

        path = (self.path or "").strip()
        if not path:
            return
        run = getattr(self, "validation_run", None)
        submission = getattr(run, "submission", None)
        if not submission or submission.file_type != SubmissionFileType.JSON:
            return
        lower = path.lower()
        prefix = "payload"
        if not lower.startswith(prefix):
            return
        remainder = path[len(prefix) :]
        if remainder and remainder[0] not in {".", "/", "["}:
            return
        remainder = remainder.lstrip("./")
        while remainder.startswith("["):
            remainder = remainder[1:]
        self.path = remainder

    def clean(self):
        super().clean()
        self._ensure_run_alignment()
        self._strip_payload_prefix()

    def save(self, *args, **kwargs):
        self._ensure_run_alignment()
        self._strip_payload_prefix()
        super().save(*args, **kwargs)


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

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.label} (run {self.validation_run_id})"

    def clean(self):
        super().clean()
        if self.org_id and self.run_id and self.org_id != self.run.org_id:
            raise ValidationError({"org": _("Artifact org must match run org.")})


class CallbackReceipt(models.Model):
    """
    Tracks processed container job callbacks for idempotency.

    When a container job called in an async manner (Cloud Run, AWS Batch)
    completes, it POSTs a callback to the worker service. The task queue may
    retry this callback if the initial delivery fails or times out, which
    could cause duplicate processing (duplicate findings, incorrect status).

    This model records each successfully processed callback. Before processing
    a callback, the handler checks if a receipt exists for the callback_id:
    - If found: return 200 OK immediately (already processed)
    - If not found: process the callback and create a receipt

    The callback_id is generated at job launch time and embedded in the input
    envelope. The container job echoes it back in the callback payload.

    Receipts are retained for debugging/audit purposes. A cleanup job can
    delete old receipts after a retention period (e.g., 30 days).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    callback_id = models.CharField(
        max_length=255,
        unique=True,
        help_text=_("Unique callback identifier from the input envelope."),
    )

    validation_run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="callback_receipts",
        help_text=_("The validation run this callback was for."),
    )

    received_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When this callback was first processed."),
    )

    # Store minimal callback data for debugging and processing state
    status = models.CharField(
        max_length=50,
        choices=CallbackReceiptStatus.choices,
        default=CallbackReceiptStatus.PROCESSING,
        help_text=_("Processing status of this callback receipt."),
    )

    result_uri = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=_("URI to the output envelope (e.g., gs:// for GCS, s3:// for S3)."),
    )

    class Meta:
        indexes = [
            models.Index(fields=["callback_id"]),
            models.Index(fields=["validation_run"]),
            models.Index(fields=["received_at"]),
        ]

    def __str__(self):
        short_id = self.callback_id[:8]
        return f"CallbackReceipt({short_id}... for run {self.validation_run_id})"


def _resource_file_upload_path(instance: ValidatorResourceFile, filename: str) -> str:
    """Generate upload path for validator resource files."""
    # Use resource file's own UUID for uniqueness (shorter path than nested UUID)
    # Format: resource_files/<resource_uuid>/<filename>
    return f"resource_files/{instance.id}/{filename}"


class ValidatorResourceFile(TimeStampedModel):
    """
    Auxiliary files needed by advanced validators to run.

    Resource files are validator-specific files (weather data, libraries, configs)
    that are not submission data but are required for validation. Examples:
    - Weather files (EPW) for EnergyPlus simulations
    - Libraries for FMU validators
    - Configuration files for custom validators

    ## Scoping

    Resource files can be system-wide or organization-specific:
    - `org=NULL`: System-wide resource, visible to all organizations
    - `org=<org>`: Organization-specific, only visible to that org

    System admins can create system-wide resources. Org admins can create
    org-specific resources.

    ## Usage Flow

    1. Admin uploads resource file via Validator Library UI
    2. Workflow step editor shows dropdown of available resources (filtered by type)
    3. User selects resource → a ``WorkflowStepResource`` row is created linking
       the step to this file (FK-backed, PROTECT on delete)
    4. At execution time, envelope builder resolves step resources to storage URIs
    5. Validator container downloads files from URIs
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    validator = models.ForeignKey(
        Validator,
        on_delete=models.CASCADE,
        related_name="resource_files",
        help_text=_("The validator this resource file is for."),
    )

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="validator_resource_files",
        null=True,
        blank=True,
        help_text=_(
            "Owning organization (NULL for system-wide resources visible to all orgs)."
        ),
    )

    resource_type = models.CharField(
        max_length=32,
        choices=ResourceFileType.choices,
        help_text=_("Type of resource (weather, library, config, etc.)."),
    )

    name = models.CharField(
        max_length=200,
        help_text=_("Human-readable name (e.g., 'San Francisco TMY3')."),
    )

    filename = models.CharField(
        max_length=255,
        help_text=_("Original filename (e.g., 'USA_CA_San.Francisco...epw')."),
    )

    file = models.FileField(
        upload_to=_resource_file_upload_path,
        max_length=500,
        help_text=_(
            "The resource file stored in default media storage (public/ prefix)."
        ),
    )

    is_default = models.BooleanField(
        default=False,
        help_text=_(
            "Mark this resource as a default when displaying to workflow authors. "
            "System defaults are shown to all orgs; org defaults only to that org."
        ),
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_resource_files",
        help_text=_("User who uploaded this resource file."),
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional description or notes about this resource."),
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Validator-specific metadata (e.g., location info for weather)."),
    )

    class Meta:
        ordering = ["-is_default", "name"]
        indexes = [
            models.Index(fields=["validator", "resource_type"]),
            models.Index(fields=["org", "resource_type"]),
        ]

    def clean(self):
        from validibot.validations.constants import get_resource_type_config

        super().clean()
        config = get_resource_type_config(self.resource_type)
        if config and self.filename:
            ext = (
                self.filename.rsplit(".", 1)[-1].lower() if "." in self.filename else ""
            )
            if ext not in config.allowed_extensions:
                allowed = ", ".join(sorted(config.allowed_extensions))
                raise ValidationError(
                    {
                        "filename": _(
                            "File extension '.%(ext)s' is not allowed for "
                            "%(type)s. Allowed: %(allowed)s."
                        )
                        % {"ext": ext, "type": config.description, "allowed": allowed},
                    },
                )

    def __str__(self):
        scope = "system" if self.org is None else f"org:{self.org_id}"
        return f"{self.name} ({self.resource_type}, {scope})"

    @property
    def is_system(self) -> bool:
        """True if this is a system-wide resource (no org)."""
        return self.org_id is None

    def get_storage_uri(self) -> str:
        """
        Get the storage URI for this resource file.

        Returns a URI that can be used by validator containers to download
        the file. The URI scheme depends on the storage backend:
        - Local storage: file:///path/to/file
        - GCS: gs://bucket/location/path/to/file

        Note: Resource files are stored via Django's FileField (media storage),
        so we use the file's actual storage path to construct the URI.

        Important: For GCS, the storage may have a `location` prefix (e.g., "public")
        that's not included in file.name. We must include it in the URI.
        """
        # Get the actual file path from Django's storage
        file_storage = self.file.storage

        # Check if this is GCS storage (django-storages GoogleCloudStorage)
        storage_class_name = file_storage.__class__.__name__
        if storage_class_name == "GoogleCloudStorage":
            # GCS: gs://bucket/location/path
            # The storage may have a location prefix (e.g., "public") that
            # isn't included in file.name but IS part of the actual object path
            bucket_name = getattr(file_storage, "bucket_name", "")
            location = getattr(file_storage, "location", "")
            if location:
                return f"gs://{bucket_name}/{location}/{self.file.name}"
            return f"gs://{bucket_name}/{self.file.name}"

        # Local filesystem storage
        # The file.path gives the absolute path
        file_path = self.file.path
        return f"file://{file_path}"
