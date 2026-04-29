from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from django.conf import settings as django_settings
from django.db import models
from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class ValidationRunStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")
    CANCELED = "CANCELED", _("Canceled")
    TIMED_OUT = "TIMED_OUT", _("Timed Out")


class ValidationRunState(TextChoices):
    """
    Public-facing lifecycle state for a validation run.

    This is intentionally separate from `ValidationRunStatus`. The underlying
    model status captures both lifecycle and terminal outcomes (for example
    `SUCCEEDED`, `FAILED`). For API consumers and the CLI we expose a simpler
    state machine:

    - `PENDING`: Run created but not yet started.
    - `RUNNING`: Run is executing.
    - `COMPLETED`: Run reached a terminal status (success, failure, cancel, timeout).
    """

    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    COMPLETED = "COMPLETED", _("Completed")


class ValidationRunResult(TextChoices):
    """
    Public-facing outcome for a validation run.

    Unlike `ValidationRunStatus`, this focuses on the terminal conclusion and is
    designed to be stable for automation (CLI exit codes, CI pipelines).
    """

    PASS = "PASS", _("Pass")
    FAIL = "FAIL", _("Fail")
    ERROR = "ERROR", _("Error")
    CANCELED = "CANCELED", _("Canceled")
    TIMED_OUT = "TIMED_OUT", _("Timed Out")
    UNKNOWN = "UNKNOWN", _("Unknown")


VALIDATION_RUN_TERMINAL_STATUSES = [
    ValidationRunStatus.SUCCEEDED,
    ValidationRunStatus.FAILED,
    ValidationRunStatus.CANCELED,
    ValidationRunStatus.TIMED_OUT,
]


def project_run_state(status: str) -> str:
    """Project the model ``ValidationRunStatus`` to public ``ValidationRunState``.

    This is the single source of truth for "is the run still going?" semantics
    across every API surface (web, REST, MCP helper, anonymous x402). The
    model column captures both lifecycle and terminal outcomes
    (PENDING / RUNNING / SUCCEEDED / FAILED / CANCELED / TIMED_OUT) but
    consumers should see the simplified state machine
    (PENDING / RUNNING / COMPLETED) plus a separate ``result`` field that
    carries the terminal outcome.

    ADR-2026-04-27 ``[trust-#6]``: the anonymous x402 status endpoint
    previously exposed ``vr.status`` verbatim under the same ``state`` key
    that the authenticated path used for the projected lifecycle value.
    The MCP server then needed a five-element ``_TERMINAL_STATES`` set that
    spanned both vocabularies. Centralising the projection here lets every
    Validibot endpoint emit one ``state`` vocabulary, so MCP, CLI, and
    future ``validibot-shared`` re-export only have to know one enum.
    """

    if status == ValidationRunStatus.PENDING:
        return ValidationRunState.PENDING
    if status == ValidationRunStatus.RUNNING:
        return ValidationRunState.RUNNING
    return ValidationRunState.COMPLETED


class ValidationRunErrorCategory(TextChoices):
    """
    Top-level classification of why a validation run failed.

    This categorizes the overall run outcome, not individual step findings.
    Used to provide human-friendly error messages and enable filtering
    in dashboards. The 'error' field on ValidationRun contains the detailed
    message; error_category classifies the type of failure.

    VALIDATION_FAILED: The validator ran successfully but found validation errors
    TIMEOUT: The validator exceeded the time limit
    OOM: The validator exceeded memory limits (container killed)
    RUNTIME_ERROR: The validator encountered an unexpected error
    SYSTEM_ERROR: Infrastructure/platform issues (storage, container runtime, etc.)
    """

    VALIDATION_FAILED = "VALIDATION_FAILED", _("Validation Failed")
    TIMEOUT = "TIMEOUT", _("Timed Out")
    OOM = "OOM", _("Out of Memory")
    RUNTIME_ERROR = "RUNTIME_ERROR", _("Runtime Error")
    SYSTEM_ERROR = "SYSTEM_ERROR", _("System Error")


# Human-friendly error messages by category
VALIDATION_RUN_ERROR_MESSAGES = {
    ValidationRunErrorCategory.VALIDATION_FAILED: (
        "The validation found issues with your file."
    ),
    ValidationRunErrorCategory.TIMEOUT: (
        "The validation took too long and was stopped. "
        "Try a smaller file or contact support for larger models."
    ),
    ValidationRunErrorCategory.OOM: (
        "The validation ran out of memory. "
        "Try a smaller file or contact support for larger models."
    ),
    ValidationRunErrorCategory.RUNTIME_ERROR: (
        "An unexpected error occurred during validation. "
        "Please try again or contact support if the problem persists."
    ),
    ValidationRunErrorCategory.SYSTEM_ERROR: (
        "A system error prevented the validation from completing. "
        "Please try again in a few minutes."
    ),
}


class LibraryLayout(TextChoices):
    GRID = "grid", _("Grid")
    LIST = "list", _("List")


VALIDATION_LIBRARY_LAYOUT_SESSION_KEY = "validation_library_layout"
VALIDATION_LIBRARY_TAB_SESSION_KEY = "validation_library_tab"


class StepStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    PASSED = "PASSED", _("Passed")
    FAILED = "FAILED", _("Failed")
    SKIPPED = "SKIPPED", _("Skipped")


class CloudRunJobStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")


class RulesetType(TextChoices):
    BASIC = "BASIC", _("Basic Assertions")
    JSON_SCHEMA = "JSON_SCHEMA", _("JSON Schema")
    XML_SCHEMA = "XML_SCHEMA", _("XML Schema")
    ENERGYPLUS = "ENERGYPLUS", _("EnergyPlus")
    FMU = "FMU", _("FMU Validator")
    CUSTOM_VALIDATOR = "CUSTOM_VALIDATOR", _("Custom Basic Validator")
    THERM = "THERM", _("THERM")


class ValidationType(TextChoices):
    """
    Validator types.

    Assertion support by type:
    - BASIC: Supports BASIC and CEL assertions against JSON payload
    - AI_ASSIST: Supports CEL assertions against JSON payload
    - ENERGYPLUS: Supports CEL assertions against output signals
    - FMU: Supports CEL assertions against output signals
    - JSON_SCHEMA: Schema-only (no assertion support)
    - XML_SCHEMA: Schema-only (no assertion support)
    - CUSTOM_VALIDATOR: Supports BASIC and CEL assertions
    - THERM: Supports CEL assertions against output signals
    """

    BASIC = "BASIC", _("Basic Assertions")
    JSON_SCHEMA = "JSON_SCHEMA", _("JSON Schema")
    XML_SCHEMA = "XML_SCHEMA", _("XML Schema")
    ENERGYPLUS = "ENERGYPLUS", _("EnergyPlus")
    FMU = "FMU", _("FMU Validator")
    CUSTOM_VALIDATOR = "CUSTOM_VALIDATOR", _("Custom Basic Validator")
    AI_ASSIST = "AI_ASSIST", _("AI Assist")
    THERM = "THERM", _("THERM Thermal Analysis")
    # SYSMLV2 = "SYSMLV2", _("SysMLv2 Model Validator")


class ValidatorReleaseState(TextChoices):
    """
    Release state for system validators.

    DRAFT: Validator is not shown anywhere in the UI. Used for validators
           still under development.
    COMING_SOON: Validator card is shown in the system library but cannot be
                 viewed or used. The "View" button shows "Coming soon" and
                 is disabled.
    PUBLISHED: Validator is fully available - viewable and usable in workflows.
    """

    DRAFT = "DRAFT", _("Draft")
    COMING_SOON = "COMING_SOON", _("Coming Soon")
    PUBLISHED = "PUBLISHED", _("Published")


# 'advanced' validation types that require dedicated compute resources —
# either container-based (EnergyPlus, FMU, custom Docker containers) or
# compute-intensive services (AI via external API calls). These are
# metered separately from simple validators that run inline in the
# Django worker process.
ADVANCED_VALIDATION_TYPES = {
    ValidationType.ENERGYPLUS,
    ValidationType.FMU,
    ValidationType.CUSTOM_VALIDATOR,
    ValidationType.AI_ASSIST,
}


class ComputeTier(models.TextChoices):
    """
    Compute intensity classification for validators.

    LOW: Lightweight operations (negligible per-run cost, metered by launch count).
    HIGH: Resource-intensive operations (metered by credit consumption).
    """

    LOW = "LOW", _("Low compute")
    HIGH = "HIGH", _("High compute")


class ValidatorWeight(models.IntegerChoices):
    """
    Credit multiplier for high-compute validators.

    Higher weight = more credits consumed per minute of runtime.
    """

    NORMAL = 1, _("Normal (1x)")
    MEDIUM = 2, _("Medium (2x)")
    HEAVY = 3, _("Heavy (3x)")
    EXTREME = 5, _("Extreme (5x)")


# Default compute tier by validation type. LOW-compute validators are metered
# by launch count; HIGH-compute validators are metered by credit consumption.
DEFAULT_COMPUTE_TIERS: dict[str, str] = {
    ValidationType.BASIC: ComputeTier.LOW,
    ValidationType.JSON_SCHEMA: ComputeTier.LOW,
    ValidationType.XML_SCHEMA: ComputeTier.LOW,
    ValidationType.CUSTOM_VALIDATOR: ComputeTier.LOW,
    ValidationType.ENERGYPLUS: ComputeTier.HIGH,
    ValidationType.FMU: ComputeTier.HIGH,
    ValidationType.THERM: ComputeTier.HIGH,
    ValidationType.AI_ASSIST: ComputeTier.LOW,
    # ValidationType.SYSMLV2: ComputeTier.LOW,
}


class FMUProbeStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")


class CustomValidatorType(TextChoices):
    SIMPLE = "SIMPLE", _("Simple")
    MODELICA = "MODELICA", _("Modelica")
    KERML = "KERML", _("KerML")


class Severity(TextChoices):
    SUCCESS = "SUCCESS", _("Success")
    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    ERROR = "ERROR", _("Error")


class ValidationRunSource(TextChoices):
    LAUNCH_PAGE = "LAUNCH_PAGE", _("Launch Page")
    API = "API", _("API")
    MCP = "MCP", _("MCP (AI Agent)")


class XMLSchemaType(TextChoices):
    DTD = "DTD", _("Document Type Definition (DTD)")
    XSD = "XSD", _("XML Schema Definition (XSD)")
    RELAXNG = "RELAXNG", _("Relax NG (RNG)")


class JSONSchemaVersion(TextChoices):
    DRAFT_2020_12 = "2020-12", _("Draft 2020-12")
    DRAFT_2019_09 = "2019-09", _("Draft 2019-09")
    DRAFT_07 = "draft-07", _("Draft 7")
    DRAFT_06 = "draft-06", _("Draft 6")
    DRAFT_04 = "draft-04", _("Draft 4")


class CatalogEntryType(TextChoices):
    SIGNAL = "signal", _("Signal")
    DERIVATION = "derivation", _("Derivation")


class CatalogRunStage(TextChoices):
    INPUT = "input", _("Input")
    OUTPUT = "output", _("Output")


class CatalogValueType(TextChoices):
    NUMBER = "number", _("Number")
    TIMESERIES = "timeseries", _("Timeseries")
    STRING = "string", _("String")
    BOOLEAN = "boolean", _("Boolean")
    OBJECT = "object", _("Object")


class SignalDirection(TextChoices):
    INPUT = "input", _("Input")
    OUTPUT = "output", _("Output")


class SignalOriginKind(TextChoices):
    CATALOG = "catalog", _("Catalog")
    FMU = "fmu", _("FMU")
    TEMPLATE = "template", _("Template")


class SignalSourceKind(TextChoices):
    PAYLOAD_PATH = "payload_path", _("Payload Path")
    INTERNAL = "internal", _("Internal")


class BindingSourceScope(TextChoices):
    SUBMISSION_PAYLOAD = "submission_payload", _("Submission Payload")
    SUBMISSION_METADATA = "submission_metadata", _("Submission Metadata")
    UPSTREAM_STEP = "upstream_step", _("Upstream Step")
    SIGNAL = "signal", _("Workflow Signal")
    SYSTEM = "system", _("System")


class AssertionType(TextChoices):
    BASIC = "basic", _("Basic Assertion")
    CEL_EXPRESSION = "cel_expr", _("CEL expression")


class ValidatorRuleType(TextChoices):
    CEL_EXPRESSION = "cel_expr", _("CEL expression")


class AssertionOperator(TextChoices):
    # Comparisons (numeric/text/temporal where applicable)
    EQ = "eq", _("Equals")
    NE = "ne", _("Not equals")
    LT = "lt", _("Less than")
    LE = "le", _("Less than or equal")  # alias for THRESHOLD_MAX UI copy
    GT = "gt", _("Greater than")
    GE = "ge", _("Greater than or equal")  # alias for THRESHOLD_MIN UI copy
    BETWEEN = "between", _("Between (range)")

    # Membership / set relations
    IN = "in", _("Is one of")
    NOT_IN = "not_in", _("Is not one of")
    SUBSET = "subset", _("Set is subset of")
    SUPERSET = "superset", _("Set is superset of")
    UNIQUE = "unique", _("All values unique")

    # String / pattern
    CONTAINS = "contains", _("Contains")
    NOT_CONTAINS = "not_contains", _("Does not contain")
    STARTS_WITH = "starts_with", _("Starts with")
    ENDS_WITH = "ends_with", _("Ends with")
    MATCHES = "matches", _("Matches regex")

    # Null/emptiness/type
    IS_NULL = "is_null", _("Is null")
    NOT_NULL = "not_null", _("Is not null")
    IS_EMPTY = "is_empty", _("Is empty")
    NOT_EMPTY = "not_empty", _("Is not empty")
    TYPE_IS = "type_is", _("Type is")

    # Length / cardinality
    LEN_EQ = "len_eq", _("Length equals")
    LEN_LE = "len_le", _("Length ≤")
    LEN_GE = "len_ge", _("Length ≥")
    COUNT_BETWEEN = "count_between", _("Count between")

    # Temporal
    BEFORE = "before", _("Before")
    AFTER = "after", _("After")
    WITHIN = "within", _("Within duration")

    # Numeric tolerance / approx
    APPROX_EQ = "approx_eq", _("≈ Equals (tolerance)")

    # Collection quantifiers
    ANY = "any", _("Any element satisfies")
    ALL = "all", _("All elements satisfy")
    NONE = "none", _("No element satisfies")
    CEL_EXPR = "cel_expr", _("CEL expression")


class ResourceFileType(TextChoices):
    """
    Types of resource files that can be attached to validators.

    Resource files are auxiliary files needed by advanced validators to run.
    Each type is specific to a validator and its requirements.

    Currently supported:
    - ENERGYPLUS_WEATHER: EPW weather files for EnergyPlus simulations

    Future types might include:
    - FMU_LIBRARY: Shared libraries for FMU validators
    - CONFIG: Configuration files
    """

    ENERGYPLUS_WEATHER = "energyplus_weather", _("EnergyPlus Weather File (EPW)")


# Step-owned resource type constants.
# These are NOT members of ResourceFileType (which is for catalog
# ValidatorResourceFile types).  They are plain string constants used as the
# ``resource_type`` value on ``WorkflowStepResource`` rows for step-owned
# files that don't belong in the shared catalog.

ENERGYPLUS_MODEL_TEMPLATE = "energyplus_model_template"
# Resource type for a parameterized IDF template uploaded by a workflow
# author.  Used on ``WorkflowStepResource`` rows with
# ``role=MODEL_TEMPLATE``.


# ---------------------------------------------------------------------------
# Resource file type configuration registry
# ---------------------------------------------------------------------------


def _validate_epw_header(raw: bytes) -> bool:
    """EPW weather files must start with 'LOCATION,'."""
    return raw[:9] == b"LOCATION,"


@dataclass(frozen=True)
class ResourceTypeConfig:
    """
    Declarative configuration for a resource file type.

    Each ResourceFileType maps to one of these configs. Adding a new resource
    type (e.g., FMU libraries) requires only adding a new entry here -- no
    form or view changes needed.
    """

    allowed_extensions: frozenset[str]
    max_size_bytes: int
    header_validator: Callable[[bytes], bool] | None = None
    description: str = ""


_RESOURCE_TYPE_CONFIGS: dict[str, ResourceTypeConfig] = {
    ResourceFileType.ENERGYPLUS_WEATHER: ResourceTypeConfig(
        allowed_extensions=frozenset({"epw"}),
        max_size_bytes=15 * 1024 * 1024,  # 15 MB
        header_validator=_validate_epw_header,
        description="EnergyPlus Weather File (EPW)",
    ),
}


def get_resource_type_config(resource_type: str) -> ResourceTypeConfig | None:
    """Look up the validation config for a resource file type."""
    return _RESOURCE_TYPE_CONFIGS.get(resource_type)


def get_resource_types_for_validator(validation_type: str) -> list[str]:
    """Return the resource file types supported by a validation type.

    Reads from the config registry. Returns an empty list if no config
    is registered or the validator doesn't use resource files.
    """
    from validibot.validations.validators.base.config import get_config

    cfg = get_config(validation_type)
    if cfg:
        return list(cfg.resource_types)
    return []


# CEL evaluation limits (adjust as needed)
# Timeout can be overridden via settings.CEL_MAX_EVAL_TIMEOUT_MS for tests.
# Default 500ms accounts for first-evaluation compilation overhead and
# large output contexts (e.g., FMU simulation results with many variables).
CEL_MAX_EVAL_TIMEOUT_MS = getattr(django_settings, "CEL_MAX_EVAL_TIMEOUT_MS", 2000)
CEL_MAX_EXPRESSION_CHARS = 2000

# Top-level variable-namespace bound. An expression can reference at
# most this many distinct top-level names (``p``, ``s``, ``output`` ...).
# Independent of — and complementary to — the deep bounds below.
CEL_MAX_CONTEXT_SYMBOLS = 200

# Maximum nesting depth of the CEL evaluation context. Mirrors the
# ``DEFAULT_MAX_DEPTH`` discipline in ``xml_utils.py``: a maliciously
# nested payload (5 MB of recursive JSON, say) balloons CPU and memory
# inside ``celpy.json_to_cel()`` during normalization, even though the
# top-level symbol count is tiny. Real CEL contexts are namespace-style
# (``s.price``, ``p.foo.bar``) and rarely exceed 5 levels; 32 is ~10x
# the realistic maximum and well below Python's recursion limit.
CEL_MAX_CONTEXT_DEPTH = 32

# Maximum total symbol count (dict keys + list items) across the entire
# context tree. Complements ``CEL_MAX_CONTEXT_SYMBOLS`` (top-level only)
# with a bounded-work guarantee for the normalization step — a context
# with one top-level key holding a 100k-entry nested structure is
# rejected before ``json_to_cel`` is called.
CEL_MAX_CONTEXT_TOTAL_SYMBOLS = 10_000

# Maximum nesting depth of CEL macros within a single expression.
# Macros (``all``, ``exists``, ``map``, ``filter``, ...) are the only
# avenue for exponential evaluation time in CEL — the cel-spec itself
# calls this out. An expression like
# ``items.all(a, items.all(b, items.all(c, ...)))`` is O(|items|^N)
# where N is the nesting depth, so even a 230-char expression with
# five levels and lists of ten is 10^5 evaluations.
#
# Two levels accommodates the common real-world intent
# (``items.all(i, i.tags.all(t, ...))``) and rejects the exponential
# pathology. Mirrors cel-go's ``ValidateComprehensionNestingLimit``.
# Chained macros (``items.all(...).filter(...)``) are additive, not
# nested, and are not counted by this limit.
CEL_MAX_MACRO_NESTING = 2

# Maximum total number of CEL macro calls anywhere in one expression.
# Chained ``.map(...).map(...).map(...)`` past five stages is never
# legitimate business logic — it is either a mistake or an attacker
# probing for cost amplification. Sits alongside CEL_MAX_MACRO_NESTING
# so that neither dimension can be exploited in isolation.
CEL_MAX_MACRO_COUNT = 5

# Regex evaluation timeout (milliseconds). Prevents ReDoS from pathological patterns.
REGEX_EVAL_TIMEOUT_MS = getattr(django_settings, "REGEX_EVAL_TIMEOUT_MS", 1000)

# Maximum number of JSONPath filter segments ([?...]) allowed in a single
# path expression. Each filter iterates an array, so chaining N filters on
# nested arrays has O(n^N) worst-case complexity. The motivating use case
# (SysML v2 named-element resolution) typically uses 1-2 filters.
MAX_JSONPATH_FILTER_SEGMENTS = 4
