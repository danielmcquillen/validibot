from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class RulesetType(TextChoices):
    """
    Enum for ruleset types.
    """

    JSON_SCHEMA = "json_schema", _("JSON Schema")
    XML_SCHEMA = "xml_schema", _("XML Schema")
    ENERGYPLUS = "energyplus", _("EnergyPlus")
    CUSTOM_RULES = "custom_rules", _("Custom Rules")


class ValidationType(TextChoices):
    """
    Enum for validation types.
    """

    JSON_SCHEMA = "json_schema", _("JSON Schema")
    XML_SCHEMA = "xml_schema", _("XML Schema")
    ENERGYPLUS = "energyplus", _("EnergyPlus")
    CUSTOM_RULES = "custom_rules", _("Custom Rules")
