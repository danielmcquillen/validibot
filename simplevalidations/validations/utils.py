import logging

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from simplevalidations.validations.constants import CustomValidatorType
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.providers import get_provider_for_validator

logger = logging.getLogger(__name__)


def create_default_validators():
    """
    Create Validator model instances for every type of validator
    we need to have by default.
    """
    from simplevalidations.validations.models import (  # noqa: PLC0415
        Validator,
        default_supported_file_types_for_validation,
    )

    default_validators = [
        {
            "name": _("Manual Assertions"),
            "slug": "basic-assertions",
            "description": _("Author assertions directly without a provider catalog."),
            "validation_type": ValidationType.BASIC,
            "version": "1.0",
            "order": 0,
            "allow_custom_assertion_targets": True,
        },
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
            "name": _("EnergyPlus Validation"),
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
        defaults = {
            **validator_data,
            "supported_file_types": default_supported_file_types_for_validation(
                validator_data["validation_type"],
            ),
        }
        validator, was_created = Validator.objects.get_or_create(
            slug=validator_data["slug"],
            defaults=defaults,
        )
        if was_created:
            created += 1
            logger.info(f"  - created default validator: {validator.slug}")
        else:
            skipped += 1

        # Update order in case it has changed
        validator.order = validator_data["order"]
        validator.is_system = True
        validator.org = None
        if not validator.supported_file_types:
            validator.supported_file_types = defaults["supported_file_types"]
        validator.allow_custom_assertion_targets = validator_data.get(
            "allow_custom_assertion_targets",
            validator.allow_custom_assertion_targets,
        )
        validator.save()

        provider = get_provider_for_validator(validator)
        if provider:
            provider.ensure_catalog_entries()

    return created, skipped


def create_custom_validator(
    *,
    org,
    user,
    name: str,
    description: str,
    custom_type: str,
    notes: str = "",
):
    """Create a custom validator and matching CustomValidator wrapper."""
    from simplevalidations.validations.models import (  # noqa: PLC0415
        CustomValidator,
        Validator,
        default_supported_file_types_for_validation,
    )

    base_validation_type = _custom_type_to_validation_type(custom_type)
    slug = _unique_validator_slug(org, name)
    validator = Validator.objects.create(
        name=name,
        description=description,
        validation_type=base_validation_type,
        org=org,
        is_system=False,
        slug=slug,
        supported_file_types=default_supported_file_types_for_validation(
            base_validation_type,
        ),
    )
    custom_validator = CustomValidator.objects.create(
        validator=validator,
        org=org,
        created_by=user,
        custom_type=custom_type,
        base_validation_type=base_validation_type,
        notes=notes,
    )
    return custom_validator


def update_custom_validator(
    custom_validator,
    *,
    name: str,
    description: str,
    notes: str,
):
    """Update validator + custom metadata."""
    validator = custom_validator.validator
    validator.name = name
    validator.description = description
    validator.save(update_fields=["name", "description", "modified"])
    custom_validator.notes = notes
    custom_validator.save(update_fields=["notes", "modified"])
    return custom_validator


def _custom_type_to_validation_type(custom_type: str) -> ValidationType:
    """Map CustomValidatorType to the corresponding ValidationType."""
    mapping = {
        CustomValidatorType.MODELICA: ValidationType.CUSTOM_RULES,
        CustomValidatorType.KERML: ValidationType.CUSTOM_RULES,
    }
    return mapping.get(custom_type, ValidationType.CUSTOM_RULES)


def _unique_validator_slug(org, name: str) -> str:
    """Generate a slug unique across validators."""
    from simplevalidations.validations.models import Validator  # noqa: PLC0415

    base = slugify(f"{org.pk}-{name}")[:50] or f"validator-{org.pk}"
    slug = base
    counter = 2
    while Validator.objects.filter(slug=slug).exists():
        slug_candidate = f"{base}-{counter}"
        slug = slug_candidate[:50]
        counter += 1
    return slug
