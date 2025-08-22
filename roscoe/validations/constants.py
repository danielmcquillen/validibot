from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class JobStatus(TextChoices):
    PENDING = "pending", _("Pending")
    RUNNING = "running", _("Running")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED = "failed", _("Failed")
    CANCELED = "canceled", _("Canceled")
    TIMED_OUT = "timed_out", _("Timed Out")


class StepStatus(TextChoices):
    PENDING = "pending", _("Pending")
    RUNNING = "running", _("Running")
    PASSED = "passed", _("Passed")
    FAILED = "failed", _("Failed")
    SKIPPED = "skipped", _("Skipped")
    # If you want a softer state later, uncomment:
    # WARNED = "warned", _("Warned")


class RulesetType(TextChoices):
    JSON_SCHEMA = "json_schema", _("JSON Schema")
    XML_SCHEMA = "xml_schema", _("XML Schema")
    ENERGYPLUS = "energyplus", _("EnergyPlus")
    CUSTOM_RULES = "custom_rules", _("Custom Rules")


class ValidationType(TextChoices):
    JSON_SCHEMA = "json_schema", _("JSON Schema")
    XML_SCHEMA = "xml_schema", _("XML Schema")
    ENERGYPLUS = "energyplus", _("EnergyPlus")
    CUSTOM_RULES = "custom_rules", _("Custom Rules")


class Severity(TextChoices):
    INFO = "info", _("Info")
    WARNING = "warning", _("Warning")
    ERROR = "error", _("Error")
