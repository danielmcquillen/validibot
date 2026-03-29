"""Validator config for the JSON Schema validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="json-schema-validator",
    name="JSON Schema Validator",
    description="Validate JSON data against a JSON Schema definition.",
    validation_type=ValidationType.JSON_SCHEMA,
    validator_class=(
        "validibot.validations.validators.json_schema.validator.JsonSchemaValidator"
    ),
    version="1.0",
    order=1,
    supported_file_types=[SubmissionFileType.JSON],
    supported_data_formats=[SubmissionDataFormat.JSON],
    allowed_extensions=["json"],
    icon="bi-filetype-json",
    card_image="JSON_SCHEMA_card_img_small.png",
)
