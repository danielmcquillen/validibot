from logging import BASIC_FORMAT

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class ValidationRunStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    SUCCEEDED = "SUCCEEDED", _("Succeeded")
    FAILED = "FAILED", _("Failed")
    CANCELED = "CANCELED", _("Canceled")
    TIMED_OUT = "TIMED_OUT", _("Timed Out")


class StepStatus(TextChoices):
    PENDING = "PENDING", _("Pending")
    RUNNING = "RUNNING", _("Running")
    PASSED = "PASSED", _("Passed")
    FAILED = "FAILED", _("Failed")
    SKIPPED = "SKIPPED", _("Skipped")


class RulesetType(TextChoices):
    JSON_SCHEMA = "JSON_SCHEMA", _("JSON Schema")
    XML_SCHEMA = "XML_SCHEMA", _("XML Schema")
    ENERGYPLUS = "ENERGYPLUS", _("EnergyPlus")
    CUSTOM_RULES = "CUSTOM_RULES", _("Custom Rules")


class ValidationType(TextChoices):
    BASIC = "BASIC", _("Basic Assertions")
    JSON_SCHEMA = "JSON_SCHEMA", _("JSON Schema")
    XML_SCHEMA = "XML_SCHEMA", _("XML Schema")
    ENERGYPLUS = "ENERGYPLUS", _("EnergyPlus")
    CUSTOM_RULES = "CUSTOM_RULES", _("Custom Rules")
    AI_ASSIST = "AI_ASSIST", _("AI Assist")


class CustomValidatorType(TextChoices):
    SIMPLE = "SIMPLE", _("Simple")
    MODELICA = "MODELICA", _("Modelica")
    KERML = "KERML", _("KerML")


class Severity(TextChoices):
    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    ERROR = "ERROR", _("Error")


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
    SIGNAL_INPUT = "signal_input", _("Signal (Input)")
    SIGNAL_OUTPUT = "signal_output", _("Signal (Output)")
    DERIVATION = "derivation", _("Derivation")


class CatalogValueType(TextChoices):
    NUMBER = "number", _("Number")
    TIMESERIES = "timeseries", _("Timeseries")
    STRING = "string", _("String")
    BOOLEAN = "boolean", _("Boolean")
    OBJECT = "object", _("Object")


class AssertionType(TextChoices):
    BASIC = "basic", _("Basic Assertion")
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
