import logging

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from simplevalidations.validations.constants import CustomValidatorType, ValidationType
from simplevalidations.validations.providers import get_provider_for_validator

logger = logging.getLogger(__name__)


def create_default_validators():
    """
    Create Validator model instances for every type of validator
    we need to have by default.
    """
    from simplevalidations.validations.models import (  # noqa: PLC0415
        Validator,
        default_supported_data_formats_for_validation,
        default_supported_file_types_for_validation,
    )

    default_validators = [
        {
            "name": _("Basic Validator"),
            "slug": "basic-validator",
            "short_description": _(
                "Author assertions directly without a provider catalog.",
            ),
            "description": _(
                """
                <p>Workflow authors can use the 'Basic Validator' as a starting point
                for creating assertions directly. There are no signals or predefined assertions.
                Perfect for lightweight checks or ad-hoc rules expressed in CEL.</p>
                """
            ),
            "validation_type": ValidationType.BASIC,
            "version": "1.0",
            "order": 0,
            "allow_custom_assertion_targets": True,
        },
        {
            "name": _("JSON Schema Validation"),
            "slug": "json-schema-validation",
            "short_description": _(
                "Validate JSON payloads against predefined schemas.",
            ),
            "description": _(
                """
                <p>
                This validator validates JSON payloads against predefined JSON schemas.
                When a workflow author selects this validator, they must attach
                a valid JSON schema. 
                </p>
                """
            ),
            "validation_type": ValidationType.JSON_SCHEMA,
            "version": "1.0",
            "order": 1,
        },
        {
            "name": _("XML Validation"),
            "slug": "xml-validation",
            "short_description": _(
                "Validate XML submissions against XSD/DTD definitions.",
            ),
            "description": _(
                """
                <p>
                This validator validates XML submissions against XSD, DTD or RelaxNG definitions.
                When a workflow author selects this validator, they must attach
                a valid schema. 
                </p>
                """
            ),
            "validation_type": ValidationType.XML_SCHEMA,
            "version": "1.0",
            "order": 2,
        },
        {
            "name": _("EnergyPlus Validation"),
            "slug": "energyplus-idf-validation",
            "short_description": _(
                "Validate EnergyPlus IDF files and outputs.",
            ),
            "description": _(
                """
                <p>Validate EnergyPlus IDF files for correctness and expected outputs. 
                "Run simulations, surface findings, and keep building models reliable.</p>
                """
            ),
            "validation_type": ValidationType.ENERGYPLUS,
            "version": "1.0",
            "order": 3,
            "has_processor": True,
        },
        {
            "name": _("FMI Validation"),
            "slug": "fmi-validation",
            "short_description": _(
                "Run FMUs and assert against inputs and outputs.",
            ),
            "description": _(
                """
                <p>Run FMUs in an isolated runtime and assert against inputs and outputs. 
                "Instrument simulations safely and share findings with collaborators.</p>
                """
            ),
            "validation_type": ValidationType.FMI,
            "version": "1.0",
            "order": 4,
            "has_processor": True,
        },
        {
            "name": _("AI Assisted Validation"),
            "slug": "ai-assisted-validation",
            "short_description": _(
                "Use AI to validate submission content against your criteria.",
            ),
            "description": _(
                """
                <p>Use AI to validate submission content against your criteria. Blend 
                traditional assertions with AI scoring to review nuanced data quickly.</p>
                """
            ),
            "validation_type": ValidationType.AI_ASSIST,
            "version": "1.0",
            "order": 5,
        },
    ]

    created = 0
    updated = 0
    for validator_data in default_validators:
        defaults = {
            **validator_data,
            "supported_data_formats": default_supported_data_formats_for_validation(
                validator_data["validation_type"]
            ),
            "supported_file_types": default_supported_file_types_for_validation(
                validator_data["validation_type"]
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
            updated += 1

        # Update order in case it has changed
        validator.order = validator_data["order"]
        validator.is_system = True
        validator.org = None
        validator.short_description = validator_data.get("short_description") or ""
        validator.description = validator_data.get("description") or ""
        if not validator.supported_file_types:
            validator.supported_file_types = defaults["supported_file_types"]
        if not validator.supported_data_formats:
            validator.supported_data_formats = defaults["supported_data_formats"]
        if validator.supported_data_formats is None:
            validator.supported_data_formats = []
        if validator.supported_file_types is None:
            validator.supported_file_types = []
        expected_formats = defaults["supported_data_formats"]
        for fmt in expected_formats:
            if fmt not in validator.supported_data_formats:
                validator.supported_data_formats.append(fmt)
        expected_file_types = defaults["supported_file_types"]
        for ft in expected_file_types:
            if ft not in validator.supported_file_types:
                validator.supported_file_types.append(ft)
        validator.has_processor = validator_data.get(
            "has_processor",
            validator.has_processor,
        )
        validator.allow_custom_assertion_targets = validator_data.get(
            "allow_custom_assertion_targets",
            validator.allow_custom_assertion_targets,
        )
        validator.save()

        provider = get_provider_for_validator(validator)
        if provider:
            provider.ensure_catalog_entries()

    return created, updated


def create_custom_validator(
    *,
    org,
    user,
    name: str,
    short_description: str = "",
    description: str,
    custom_type: str,
    notes: str = "",
    version: str | None = "",
    allow_custom_assertion_targets: bool = False,
    supported_data_formats: list[str] | None = None,
):
    """Create a custom validator and matching CustomValidator wrapper."""
    from simplevalidations.validations.models import (  # noqa: PLC0415
        CustomValidator,
        Validator,
        default_supported_data_formats_for_validation,
        default_supported_file_types_for_validation,
        supported_file_types_for_data_formats,
    )

    base_validation_type = _custom_type_to_validation_type(custom_type)
    slug = _unique_validator_slug(org, name)
    data_formats = (
        list(supported_data_formats)
        if supported_data_formats
        else default_supported_data_formats_for_validation(base_validation_type)
    )
    file_types = supported_file_types_for_data_formats(data_formats) or (
        default_supported_file_types_for_validation(base_validation_type)
    )
    validator = Validator.objects.create(
        name=name,
        short_description=short_description,
        description=description,
        validation_type=base_validation_type,
        org=org,
        is_system=False,
        slug=slug,
        supported_data_formats=data_formats,
        supported_file_types=file_types,
        allow_custom_assertion_targets=allow_custom_assertion_targets,
        version=version or "",
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
    short_description: str,
    description: str,
    notes: str,
    version: str | None = "",
    allow_custom_assertion_targets: bool | None = None,
    supported_data_formats: list[str] | None = None,
):
    """Update validator + custom metadata."""
    from simplevalidations.validations.models import (  # noqa: PLC0415
        supported_file_types_for_data_formats,
    )

    validator = custom_validator.validator
    validator.name = name
    validator.short_description = short_description
    validator.description = description
    if version is not None:
        validator.version = version
    if allow_custom_assertion_targets is not None:
        validator.allow_custom_assertion_targets = allow_custom_assertion_targets
    if supported_data_formats:
        validator.supported_data_formats = list(supported_data_formats)
        validator.supported_file_types = supported_file_types_for_data_formats(
            validator.supported_data_formats,
        )
    validator.save(
        update_fields=[
            "name",
            "short_description",
            "description",
            "version",
            "allow_custom_assertion_targets",
            "supported_data_formats",
            "supported_file_types",
            "modified",
        ],
    )
    custom_validator.notes = notes
    custom_validator.save(update_fields=["notes", "modified"])
    return custom_validator


def _custom_type_to_validation_type(custom_type: str) -> ValidationType:
    """Map CustomValidatorType to the corresponding ValidationType."""
    mapping = {
        CustomValidatorType.MODELICA: ValidationType.CUSTOM_VALIDATOR,
        CustomValidatorType.KERML: ValidationType.CUSTOM_VALIDATOR,
    }
    return mapping.get(custom_type, ValidationType.CUSTOM_VALIDATOR)


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
