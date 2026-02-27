"""
Configs for built-in validators that are single-file modules.

These validators (Basic, JSON Schema, XML Schema, AI Assist, Custom) are
simple enough to be single Python files rather than sub-packages. They
don't have their own ``config.py`` modules, so their metadata is declared
here instead.

The config registry pulls from both ``discover_configs()`` (for package-
based validators with ``config.py``) and this module (for single-file
built-in validators).
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

BUILTIN_CONFIGS: list[ValidatorConfig] = [
    ValidatorConfig(
        slug="basic-validator",
        name="Basic Validator",
        description=(
            "The simplest validator. Allows workflow author to add signals"
            " and assertions directly without a validator catalog."
        ),
        validation_type=ValidationType.BASIC,
        version="1.0",
        order=0,
        supported_file_types=[
            SubmissionFileType.JSON,
            SubmissionFileType.XML,
            SubmissionFileType.TEXT,
            SubmissionFileType.YAML,
        ],
        supported_data_formats=[
            SubmissionDataFormat.JSON,
            SubmissionDataFormat.XML,
            SubmissionDataFormat.TEXT,
            SubmissionDataFormat.YAML,
        ],
        allowed_extensions=["json", "xml", "txt", "yaml", "yml"],
        icon="bi-journal-bookmark",
        card_image="BASIC_card_img_small.png",
    ),
    ValidatorConfig(
        slug="json-schema-validator",
        name="JSON Schema Validator",
        description="Validate JSON data against a JSON Schema definition.",
        validation_type=ValidationType.JSON_SCHEMA,
        version="1.0",
        order=1,
        supported_file_types=[SubmissionFileType.JSON],
        supported_data_formats=[SubmissionDataFormat.JSON],
        allowed_extensions=["json"],
        icon="bi-filetype-json",
        card_image="JSON_SCHEMA_card_img_small.png",
    ),
    ValidatorConfig(
        slug="xml-schema-validator",
        name="XML Schema Validator",
        description="Validate XML data against XSD, RelaxNG, or DTD schemas.",
        validation_type=ValidationType.XML_SCHEMA,
        version="1.0",
        order=2,
        supported_file_types=[SubmissionFileType.XML],
        supported_data_formats=[SubmissionDataFormat.XML],
        allowed_extensions=["xml", "xsd", "rng", "dtd"],
        icon="bi-filetype-xml",
        card_image="XML_SCHEMA_card_img_small.png",
    ),
    ValidatorConfig(
        slug="ai-assisted-validator",
        name="AI Assisted Validator",
        description="AI-powered validation using language models.",
        validation_type=ValidationType.AI_ASSIST,
        version="1.0",
        order=5,
        supported_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
        supported_data_formats=[SubmissionDataFormat.JSON, SubmissionDataFormat.TEXT],
        allowed_extensions=["json", "txt"],
        icon="bi-robot",
        card_image="AI_ASSIST_card_img_small.png",
    ),
    ValidatorConfig(
        slug="custom-validator",
        name="Custom Validator",
        description="User-defined validator with custom container logic.",
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        version="1.0",
        order=99,
        is_system=False,
        supported_file_types=[
            SubmissionFileType.JSON,
            SubmissionFileType.TEXT,
            SubmissionFileType.YAML,
        ],
        supported_data_formats=[SubmissionDataFormat.JSON],
        allowed_extensions=["json", "yaml", "yml"],
    ),
]
