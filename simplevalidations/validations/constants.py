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
    JSON_SCHEMA = "JSON_SCHEMA", _("JSON Schema")
    XML_SCHEMA = "XML_SCHEMA", _("XML Schema")
    ENERGYPLUS = "ENERGYPLUS", _("EnergyPlus")
    CUSTOM_RULES = "CUSTOM_RULES", _("Custom Rules")
    AI_ASSIST = "AI_ASSIST", _("AI Assist")


class CustomValidatorType(TextChoices):
    MODELICA = "MODELICA", _("Modelica")
    PYWINCALC = "PYWINCALC", _("PyWinCalc")


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
