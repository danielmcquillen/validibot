import logging

from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from validibot.validations.constants import CustomValidatorType
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorReleaseState

logger = logging.getLogger(__name__)


def create_default_validators():
    """
    Create Validator model instances for every type of validator
    we need to have by default.
    """
    from validibot.validations.models import Validator
    from validibot.validations.models import (
        default_supported_data_formats_for_validation,
    )
    from validibot.validations.models import default_supported_file_types_for_validation

    default_validators = [
        {
            "name": _("Basic Validator"),
            "slug": "basic-validator",
            "short_description": _(
                "The simplest validator. "
                "Allows workflow author to add signals and assertions directly "
                "without a validator catalog.",
            ),
            "description": _(
                """
                <p>Workflow authors can use the 'Basic Validator' as a starting point
                for creating assertions directly. There are no signals
                or predefined assertions.
                Perfect for lightweight checks or ad-hoc rules expressed in CEL.</p>
                """
            ),
            "validation_type": ValidationType.BASIC,
            "version": "1.0",
            "order": 0,
            "allow_custom_assertion_targets": True,
        },
        {
            "name": _("JSON Schema Validator"),
            "slug": "json-schema-validator",
            "short_description": _(
                "Validate JSON payloads against a JSON schema provided "
                "by the workflow author.",
            ),
            "description": _(
                """
                <p>
                This validator validates JSON payloads against a predefined JSON schema.
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
            "name": _("XML Validator"),
            "slug": "xml-validator",
            "short_description": _(
                "Validate XML submissions against a XSD, DTD, or RelaxNG "
                "schema provided by the workflow author.",
            ),
            "description": _(
                """
                <p>
                This validator validates XML submissions against an XSD, DTD
                or RelaxNG schema.
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
            "name": _("EnergyPlus Validator"),
            "slug": "energyplus-idf-validator",
            "short_description": _(
                "Validate EnergyPlus IDF files and outputs.",
            ),
            "description": _(
                """
                <p>Validate EnergyPlus IDF files for correctness and expected outputs.
                Run simulations, surface findings, and keep building models
                reliable.</p>
                """
            ),
            "validation_type": ValidationType.ENERGYPLUS,
            "version": "1.0",
            "order": 3,
            "has_processor": True,
        },
        {
            "name": _("FMU Validator"),
            "slug": "fmi-validator",
            "short_description": _(
                "Run FMUs and assert against inputs and outputs.",
            ),
            "description": _(
                """
                <p>
                This validator allows a workflow author to write assertions
                against incoming data.
                It allows a workflow author to validating incoming data as
                well as simulation outputs.
                </p>
                <p>
                The validator sends inputs using the Functional Mock-up Interface (FMI)
                standard to an
                FMU-based simulation running in an isolated runtime. If the
                simulation succeeds it
                gathers outputs and returns them as output signals for further
                validation, if defined.
                </p>
                <p>
                The workflow author to write assertions against simulation
                output signals.
                </p>
                """
            ),
            "validation_type": ValidationType.FMU,
            "version": "1.0",
            "order": 4,
            "has_processor": True,
        },
        {
            "name": _("AI Assisted Validator"),
            "slug": "ai-assisted-validator",
            "short_description": _(
                "Use AI to validate submission content against your criteria.",
            ),
            "description": _(
                """
                <p>Use AI to validate submission content against your criteria. Blend
                traditional assertions with AI scoring to review nuanced data quickly.
                </p>
                """
            ),
            "validation_type": ValidationType.AI_ASSIST,
            "version": "1.0",
            "order": 5,
            "release_state": ValidatorReleaseState.COMING_SOON,
        },
        {
            "name": _("THERM Validator"),
            "slug": "therm-validator",
            "short_description": _(
                "Validate THERM thermal analysis files (THMX/THMZ) "
                "for geometry, materials, and boundary conditions.",
            ),
            "description": _(
                """
                <p>Validate LBNL THERM files before submission to NFRC or
                other certification bodies. Checks geometry closure, material
                property ranges, boundary condition completeness, and
                reference integrity. Extracts signals for downstream
                compliance assertions (e.g. NFRC 100 winter conditions).</p>
                """
            ),
            "validation_type": ValidationType.THERM,
            "version": "1.0",
            "order": 6,
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
        validator.name = validator_data["name"]
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
        # Set release_state from config, defaulting to PUBLISHED for system validators
        validator.release_state = validator_data.get(
            "release_state",
            ValidatorReleaseState.PUBLISHED,
        )
        validator.save()

        # Ensure every system validator has a default_ruleset for holding
        # validator-level assertions. The save() method calls
        # ensure_default_ruleset(), but for validators created before this
        # feature existed we also call it explicitly here.
        validator.ensure_default_ruleset()

    # Note: Catalog entries for advanced validators are synced separately via:
    #   python manage.py sync_advanced_validators
    # This function only creates the validator instances.

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
    from validibot.validations.models import CustomValidator
    from validibot.validations.models import Validator
    from validibot.validations.models import (
        default_supported_data_formats_for_validation,
    )
    from validibot.validations.models import default_supported_file_types_for_validation
    from validibot.validations.models import supported_file_types_for_data_formats

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
    from validibot.validations.models import supported_file_types_for_data_formats

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
    from validibot.validations.models import Validator

    base = slugify(f"{org.pk}-{name}")[:50] or f"validator-{org.pk}"
    slug = base
    counter = 2
    while Validator.objects.filter(slug=slug).exists():
        slug_candidate = f"{base}-{counter}"
        slug = slug_candidate[:50]
        counter += 1
    return slug
