from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from functools import cached_property

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Value
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel
from slugify import slugify

from simplevalidations.projects.models import Project
from simplevalidations.submissions.constants import (
    SubmissionDataFormat,
    SubmissionFileType,
    data_format_allowed_file_types,
)
from simplevalidations.submissions.models import Submission
from simplevalidations.users.models import Organization, User
from simplevalidations.validations.constants import (
    AssertionOperator,
    AssertionType,
    CatalogEntryType,
    CatalogRunStage,
    CatalogValueType,
    CustomValidatorType,
    FMUProbeStatus,
    JSONSchemaVersion,
    RulesetType,
    Severity,
    StepStatus,
    ValidationRunSource,
    ValidationRunStatus,
    ValidationType,
    ValidatorRuleType,
    XMLSchemaType,
)
from simplevalidations.workflows.models import Workflow, WorkflowStep

VALIDATION_TYPE_FILE_TYPE_DEFAULTS = {
    ValidationType.BASIC: [
        SubmissionFileType.JSON,
        SubmissionFileType.XML,
        SubmissionFileType.TEXT,
        SubmissionFileType.YAML,
    ],
    ValidationType.JSON_SCHEMA: [
        SubmissionFileType.JSON,
    ],
    ValidationType.XML_SCHEMA: [
        SubmissionFileType.XML,
    ],
    ValidationType.ENERGYPLUS: [
        SubmissionFileType.TEXT,
        SubmissionFileType.JSON,
    ],
    ValidationType.FMI: [
        SubmissionFileType.BINARY,
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
    ],
    ValidationType.CUSTOM_VALIDATOR: [
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
        SubmissionFileType.YAML,
    ],
    ValidationType.AI_ASSIST: [
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
    ],
}


VALIDATION_TYPE_DATA_FORMAT_DEFAULTS = {
    ValidationType.BASIC: [
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.XML,
        SubmissionDataFormat.YAML,
        SubmissionDataFormat.TEXT,
    ],
    ValidationType.JSON_SCHEMA: [SubmissionDataFormat.JSON],
    ValidationType.XML_SCHEMA: [SubmissionDataFormat.XML],
    ValidationType.ENERGYPLUS: [
        SubmissionDataFormat.ENERGYPLUS_IDF,
        SubmissionDataFormat.ENERGYPLUS_EPJSON,
    ],
    ValidationType.FMI: [
        SubmissionDataFormat.FMU,
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.TEXT,
    ],
    ValidationType.CUSTOM_VALIDATOR: [
        SubmissionDataFormat.JSON,
    ],
    ValidationType.AI_ASSIST: [
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.TEXT,
    ],
}


def default_supported_file_types_for_validation(
    validation_type: str,
) -> list[str]:
    derived_formats = default_supported_data_formats_for_validation(validation_type)
    derived = supported_file_types_for_data_formats(derived_formats)
    explicit = VALIDATION_TYPE_FILE_TYPE_DEFAULTS.get(validation_type, [])
    merged: list[str] = []
    for value in (derived or []) + list(explicit):
        if value not in merged:
            merged.append(value)
    return merged or [SubmissionFileType.JSON]


def default_supported_data_formats_for_validation(validation_type: str) -> list[str]:
    return list(
        VALIDATION_TYPE_DATA_FORMAT_DEFAULTS.get(
            validation_type,
            [SubmissionDataFormat.JSON],
        ),
    )


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

    target_catalog_entry = models.ForeignKey(
        "validations.ValidatorCatalogEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
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
            "Operator options (inclusive bounds, tolerance "
            "metadata, case folding, etc.).",
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
                condition=(
                    Q(target_catalog_entry__isnull=False, target_field="")
                    | (Q(target_catalog_entry__isnull=True) & ~Q(target_field=""))
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["ruleset", "order"]),
            models.Index(fields=["operator"]),
        ]

    def __str__(self):
        target = (
            self.target_catalog_entry.slug
            if self.target_catalog_entry_id
            else self.target_field
        )
        return f"{self.ruleset_id}:{self.operator}:{target or '?'}"

    @property
    def resolved_run_stage(self) -> CatalogRunStage:
        if self.target_catalog_entry_id and self.target_catalog_entry:
            return CatalogRunStage(self.target_catalog_entry.run_stage)
        return CatalogRunStage.OUTPUT

    def clean(self):
        super().clean()
        catalog_set = bool(self.target_catalog_entry_id)
        field = (self.target_field or "").strip()
        if catalog_set == bool(field):
            raise ValidationError(
                {
                    "target_field": _(
                        "Provide either a catalog target or a "
                        "custom path (but not both).",
                    ),
                },
            )

    @property
    def target_display(self) -> str:
        if self.target_catalog_entry_id and self.target_catalog_entry:
            label = self.target_catalog_entry.label or self.target_catalog_entry.slug
            return f"{label} ({self.target_catalog_entry.slug})"
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


@dataclass(frozen=True)
class CatalogDisplay:
    entries: list[ValidatorCatalogEntry]
    inputs: list[ValidatorCatalogEntry]
    outputs: list[ValidatorCatalogEntry]
    input_derivations: list[ValidatorCatalogEntry]
    output_derivations: list[ValidatorCatalogEntry]
    input_total: int
    output_total: int
    uses_tabs: bool


def _default_validator_file_types() -> list[str]:
    return [SubmissionFileType.JSON]


def _default_validator_data_formats() -> list[str]:
    return [SubmissionDataFormat.JSON]


def _fmu_upload_path(instance: "FMUModel", filename: str) -> str:
    org_segment = instance.org_id or "system"
    return f"fmu/{org_segment}/{uuid.uuid4()}-{filename}"


class FMUModel(TimeStampedModel):
    """
    Stored FMU artifact plus parsed metadata used by FMI validators.

    The FMU never executes inside Django; we store it for Modal runners to
    download and for offline inspection/probe runs. Each FMU also records a
    checksum and Modal Volume path so the Modal runtime can reuse a cached
    copy keyed by checksum.
    """

    class FMIKind(models.TextChoices):
        MODEL_EXCHANGE = "ModelExchange", _("Model Exchange")
        CO_SIMULATION = "CoSimulation", _("Co-Simulation")

    class FMIVersion(models.TextChoices):
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
        help_text=_("SHA256 checksum used to reference cached FMUs in Modal."),
    )
    modal_volume_path = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_("Path inside the Modal Volume cache for this FMU."),
    )
    fmi_version = models.CharField(
        max_length=8,
        choices=FMIVersion.choices,
        default=FMIVersion.V2_0,
    )
    kind = models.CharField(
        max_length=32,
        choices=FMIKind.choices,
        default=FMIKind.CO_SIMULATION,
    )
    is_approved = models.BooleanField(default=False)
    size_bytes = models.BigIntegerField(default=0)
    introspection_metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.name} ({self.fmi_version}, {self.kind})"


class FMIVariable(TimeStampedModel):
    """
    Parsed variable metadata from modelDescription.xml attached to an FMUModel.

    These rows mirror ScalarVariable definitions so catalog entries can point
    at stable FMU variable names.
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
    catalog_entry = models.ForeignKey(
        "validations.ValidatorCatalogEntry",
        on_delete=models.SET_NULL,
        related_name="fmi_variables",
        null=True,
        blank=True,
    )

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
            "A brief summary of the validator's purpose. This description appears in lists and cards."
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
            "True when the validator includes an intermediate processor that produces output signals."
        ),
    )
    fmu_model = models.ForeignKey(
        "validations.FMUModel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validators",
        help_text=_(
            "FMU artifact backing this validator (only used for FMI validators).",
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

    allow_custom_assertion_targets = models.BooleanField(
        default=False,
        help_text=_(
            "Allow authors to enter assertion targets not present in the catalog.",
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

    @property
    def card_image_name(self) -> str:
        if self.validation_type:
            return f"{self.validation_type}_card_img_small.png"
        return "default_card_img_small.png"

    @property
    def display_icon(self) -> str:
        bi_icon_class = {
            ValidationType.JSON_SCHEMA: "bi-filetype-json",
            ValidationType.XML_SCHEMA: "bi-filetype-xml",
            ValidationType.ENERGYPLUS: "bi-lightning-charge-fill",
            ValidationType.FMI: "bi-cpu",
            ValidationType.AI_ASSIST: "bi-robot",
        }.get(self.validation_type, "bi-journal-bookmark")  # default icon
        return bi_icon_class

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

        if self.validation_type == ValidationType.FMI:
            if not self.fmu_model_id:
                if not self.is_system:
                    raise ValidationError(
                        {
                            "fmu_model": _(
                                "Assign an FMU asset before saving an FMI validator.",
                            ),
                        },
                    )
        elif self.fmu_model_id:
            raise ValidationError(
                {
                    "fmu_model": _(
                        "FMU assets can only be attached to FMI validators.",
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

    @property
    def is_custom(self) -> bool:
        return bool(self.org_id and not self.is_system)

    def catalog_entries_by_type(self) -> dict[str, list[ValidatorCatalogEntry]]:
        include_derivations = getattr(settings, "ENABLE_DERIVED_SIGNALS", False)
        grouped: dict[str, list[ValidatorCatalogEntry]] = {
            CatalogEntryType.SIGNAL: [],
        }
        if include_derivations:
            grouped[CatalogEntryType.DERIVATION] = []
        for entry in self.catalog_entries.all().order_by("order", "slug"):
            if (
                not include_derivations
                and entry.entry_type == CatalogEntryType.DERIVATION
            ):
                continue
            grouped.setdefault(entry.entry_type, []).append(entry)
        return grouped

    def catalog_entries_by_stage(self) -> dict[str, list[ValidatorCatalogEntry]]:
        grouped: dict[str, list[ValidatorCatalogEntry]] = {
            CatalogRunStage.INPUT: [],
            CatalogRunStage.OUTPUT: [],
        }
        qs = self.catalog_entries.filter(entry_type=CatalogEntryType.SIGNAL).order_by(
            "run_stage",
            "order",
            "slug",
        )
        for entry in qs:
            grouped.setdefault(entry.run_stage, []).append(entry)
        return grouped

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

    def has_signal_stage(self, stage: CatalogRunStage) -> bool:
        return self.catalog_entries.filter(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=stage,
        ).exists()

    def has_signal_stages(self) -> bool:
        return self.has_signal_stage(CatalogRunStage.INPUT) and self.has_signal_stage(
            CatalogRunStage.OUTPUT,
        )

    @cached_property
    def catalog_display(self) -> CatalogDisplay:
        include_derivations = getattr(settings, "ENABLE_DERIVED_SIGNALS", False)
        entries = list(
            self.catalog_entries.all().order_by(
                "entry_type",
                "run_stage",
                "order",
                "slug",
            ),
        )
        if not include_derivations:
            entries = [
                entry
                for entry in entries
                if entry.entry_type == CatalogEntryType.SIGNAL
            ]
        inputs = [
            entry
            for entry in entries
            if entry.entry_type == CatalogEntryType.SIGNAL
            and entry.run_stage == CatalogRunStage.INPUT
        ]
        outputs = [
            entry
            for entry in entries
            if entry.entry_type == CatalogEntryType.SIGNAL
            and entry.run_stage == CatalogRunStage.OUTPUT
        ]
        input_derivations: list[ValidatorCatalogEntry] = []
        output_derivations: list[ValidatorCatalogEntry] = []
        if include_derivations:
            input_derivations = [
                entry
                for entry in entries
                if entry.entry_type == CatalogEntryType.DERIVATION
                and entry.run_stage == CatalogRunStage.INPUT
            ]
            output_derivations = [
                entry
                for entry in entries
                if entry.entry_type == CatalogEntryType.DERIVATION
                and entry.run_stage == CatalogRunStage.OUTPUT
            ]
        input_total = len(inputs) + len(input_derivations)
        output_total = len(outputs) + len(output_derivations)
        total_entries = len(entries)
        uses_tabs = bool(inputs and outputs) or total_entries == 0
        return CatalogDisplay(
            entries=entries,
            inputs=inputs,
            outputs=outputs,
            input_derivations=input_derivations,
            output_derivations=output_derivations,
            input_total=input_total,
            output_total=output_total,
            uses_tabs=uses_tabs,
        )

    def get_catalog_entries(
        self,
        *,
        entry_type: CatalogEntryType | None = None,
    ) -> models.QuerySet[ValidatorCatalogEntry]:
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
        help_text=_(
            "Phase of the validator run when this entry is available. INPUT is before the validator's processor operates on the data. OUTPUT is the data generated by the validator's processor."
        ),  # noqa: E501
    )

    slug = models.SlugField(
        max_length=255,
        # Keep this as the stable identifier used in rules/CEL; exposed as "Signal name" in the UI
        # to reduce confusion with the target path (which can include dots/brackets and may change).
        help_text=_(
            "Unique name for this catalog entry within the validator. You use this name in assertions and CEL statements."
        ),
    )

    label = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_("Human-friendly label shown in editors."),
    )

    target_field = models.CharField(
        max_length=255,
        help_text=_(
            "Path used to locate this signal in the input or processor output."
        ),
    )

    data_type = models.CharField(
        max_length=32,
        choices=CatalogValueType.choices,
        default=CatalogValueType.NUMBER,
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "A short description to help you remember what data this signal represents."
        ),
    )

    target_field = models.CharField(
        max_length=255,
        help_text=_(
            "Path used to locate this signal in the input or processor output."
        ),
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
        help_text=_(
            "Requires the signal to be present in the submission (inputs) or processor output (outputs)."
        ),
    )

    # Why have an is_hidden field?
    # We hide some signals to keep the authoring UI clean and to avoid exposing internal or
    # potentially confusing data, while still letting CEL use them when needed.
    #
    # Typical cases:
    # - Provider/internal signals needed for evaluation (e.g., derived metrics, instrumentation metadata) that
    #   authors shouldn’t edit/select.
    # - Signals used to render messages or do bookkeeping (like run IDs, internal flags)
    #   where showing them would clutter the picker or leak implementation details.
    # - Back-compat: keep a signal available for rules that reference it without encouraging new authors to depend on it.
    is_hidden = models.BooleanField(
        default=False,
        help_text=_(
            "Hidden signals remain available to bindings but are not shown in authoring interfaces.",
        ),
    )

    default_value = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text=_("Optional default applied when the signal is hidden."),
    )

    order = models.PositiveIntegerField(default=0)

    def delete(self, *args, **kwargs):
        if self.referencing_rules.exists():
            raise ValidationError(
                {
                    "catalog_entry": _(
                        "Cannot delete a signal/derivation referenced by rules.",
                    ),
                },
            )
        return super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.entry_type == CatalogEntryType.DERIVATION:
            self.is_required = False
        if not self.is_hidden:
            self.default_value = None

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["validator", "entry_type", "run_stage", "slug"],
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
        on_delete=models.CASCADE,
        related_name="runs",
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

    resolved_config = models.JSONField(
        default=dict,
        blank=True,
    )  # effective per-run config snapshot

    source = models.CharField(
        max_length=32,
        choices=ValidationRunSource.choices,
        default=ValidationRunSource.LAUNCH_PAGE,
        help_text=_("Where this run was initiated (web launch page, API, etc.)."),
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
            name = f"Results from Step {self.workflow_step.step_number} : {self.workflow_step.name}"
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
    Normalized issues produced by step runs for efficient filtering/pagination.
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


class ValidatorCatalogRule(TimeStampedModel):
    """
    Default assertion defined at the validator level (for example, CEL expressions).

    Default assertions run automatically whenever the validator executes and can
    reference one or more catalog entries. The meaning of an assertion is driven by
    ``rule_type`` and the stored ``expression``/``metadata``.
    """

    class Meta:
        ordering = ["order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["validator", "name"],
                name="uq_validator_rule_name",
            ),
            models.CheckConstraint(
                name="ck_validator_rule_order_nonnegative",
                condition=models.Q(order__gte=0),
            ),
        ]

    validator = models.ForeignKey(
        "Validator",
        on_delete=models.CASCADE,
        related_name="rules",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    rule_type = models.CharField(
        max_length=32,
        choices=ValidatorRuleType.choices,
        default=ValidatorRuleType.CEL_EXPRESSION,
    )
    expression = models.TextField(
        help_text=_("Rule definition (e.g., CEL expression)."),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Additional structured data for this rule."),
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text=_("Relative ordering for evaluation/display."),
    )

    def clean(self):
        super().clean()
        if not self.name or not self.name.strip():
            raise ValidationError({"name": _("Default assertion name is required.")})
        if not self.expression or not str(self.expression).strip():
            raise ValidationError(
                {"expression": _("Default assertion expression is required.")}
            )

    def __str__(self):
        return f"{self.validator.slug}:{self.name}"


class ValidatorCatalogRuleEntry(models.Model):
    """
    Join table linking default assertions to catalog entries they reference.
    """

    class Meta:
        unique_together = (
            "rule",
            "catalog_entry",
        )
        indexes = [
            models.Index(
                fields=[
                    "rule",
                    "catalog_entry",
                ],
            ),
        ]

    rule = models.ForeignKey(
        ValidatorCatalogRule,
        on_delete=models.CASCADE,
        related_name="rule_entries",
    )
    catalog_entry = models.ForeignKey(
        ValidatorCatalogEntry,
        on_delete=models.CASCADE,
        related_name="referencing_rules",
    )


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
