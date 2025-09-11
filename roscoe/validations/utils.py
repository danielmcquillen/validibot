from django.utils.translation import gettext_lazy as _

from roscoe.validations.constants import ValidationType


def create_default_validators():
    """
    Create Validator model instances for every type of validator
    we need to have by default.
    """
    from roscoe.validations.models import Validator

    default_validators = [
        {
            "name": _("JSON Schema Validation"),
            "slug": "json-schema-validation",
            "description": _("Validate JSON payload against a predefined schema."),
            "validation_type": ValidationType.JSON_SCHEMA,
            "version": "1.0",
            "is_active": True,
        },
        {
            "name": _("XML Validation"),
            "slug": "xml-validation",
            "description": _("Validate XML payload against a predefined schema."),
            "validation_type": ValidationType.XML_SCHEMA,
            "version": "1.0",
            "is_active": True,
        },
    ]

    for validator_data in default_validators:
        Validator.objects.get_or_create(
            slug=validator_data["slug"],
            defaults=validator_data,
        )
