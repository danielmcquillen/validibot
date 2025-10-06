import logging

from django.utils.translation import gettext_lazy as _

from simplevalidations.validations.constants import ValidationType

logger = logging.getLogger(__name__)


def create_default_validators():
    """
    Create Validator model instances for every type of validator
    we need to have by default.
    """
    from simplevalidations.validations.models import Validator  # noqa: PLC0415

    default_validators = [
        {
            "name": _("JSON Schema Validation"),
            "slug": "json-schema-validation",
            "description": _("Validate JSON payload against a predefined schema."),
            "validation_type": ValidationType.JSON_SCHEMA,
            "version": "1.0",
            "order": 1,
        },
        {
            "name": _("XML Validation"),
            "slug": "xml-validation",
            "description": _("Validate XML payload against a predefined schema."),
            "validation_type": ValidationType.XML_SCHEMA,
            "version": "1.0",
            "order": 2,
        },
        {
            "name": _("EnergyPlus IDF Validation"),
            "slug": "energyplus-idf-validation",
            "description": _(
                "Validate an EnergyPlus IDF file for correctness and output values."
            ),
            "validation_type": ValidationType.ENERGYPLUS,
            "version": "1.0",
            "order": 3,
        },
        {
            "name": _("AI Assisted Validation"),
            "slug": "ai-assisted-validation",
            "description": _(
                "Use AI to validate the submission content based on custom criteria."
            ),
            "validation_type": ValidationType.AI_ASSIST,
            "version": "1.0",
            "order": 4,
        },
    ]

    created = 0
    skipped = 0
    for validator_data in default_validators:
        validator, was_created = Validator.objects.get_or_create(
            slug=validator_data["slug"],
            defaults=validator_data,
        )
        if was_created:
            created += 1
            logger.info(f"  - created default validator: {validator.slug}")
        else:
            skipped += 1
            
        # Update order in case it has changed
        validator.order = validator_data["order"]
        validator.save()

    return created, skipped
