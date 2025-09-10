from validations.constants import ValidationType


def create_default_validators():
    """
    Create Validator model instances for every type of validator
    we need to have by default.
    """
    from roscoe.validations.models import Validator

    default_validators = [
        {
            "name": "JSON Schema Validation",
            "slug": "json-schema-validation",
            "description": "Validate JSON payload against a predefined schema.",
            "validation_type": ValidationType.JSON_SCHEMA,
            "is_active": True,
        },
        {
            "name": "XSL Validation",
            "slug": "xsl-validation",
            "description": "Validate XML payload against a predefined schema.",
            "validation_type": ValidationType.XML_SCHEMA,
            "is_active": True,
        },
    ]

    for validator_data in default_validators:
        Validator.objects.get_or_create(
            slug=validator_data["slug"],
            defaults=validator_data,
        )
