from django.conf import settings as django_settings
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
    SYSTEM_ERROR: Infrastructure/platform issues (GCS, Cloud Run, etc.)
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
    FMI = "FMI", _("FMU Validator")
    CUSTOM_VALIDATOR = "CUSTOM_VALIDATOR", _("Custom Basic Validator")


class ValidationType(TextChoices):
    BASIC = "BASIC", _("Basic Assertions")
    JSON_SCHEMA = "JSON_SCHEMA", _("JSON Schema")
    XML_SCHEMA = "XML_SCHEMA", _("XML Schema")
    ENERGYPLUS = "ENERGYPLUS", _("EnergyPlus")
    FMI = "FMI", _("FMU Validator")
    CUSTOM_VALIDATOR = "CUSTOM_VALIDATOR", _("Custom Basic Validator")
    AI_ASSIST = "AI_ASSIST", _("AI Assist")


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


# 'advanced' validation types that may require more resources or have special handling
ADVANCED_VALIDATION_TYPES = {
    ValidationType.ENERGYPLUS,
    ValidationType.FMI,
    ValidationType.CUSTOM_VALIDATOR,
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
    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    ERROR = "ERROR", _("Error")


class ValidationRunSource(TextChoices):
    LAUNCH_PAGE = "LAUNCH_PAGE", _("Launch Page")
    API = "API", _("API")


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


# CEL evaluation limits (adjust as needed)
# Timeout can be overridden via settings.CEL_MAX_EVAL_TIMEOUT_MS for tests
CEL_MAX_EVAL_TIMEOUT_MS = getattr(django_settings, "CEL_MAX_EVAL_TIMEOUT_MS", 100)
CEL_MAX_EXPRESSION_CHARS = 2000
CEL_MAX_CONTEXT_SYMBOLS = 200
