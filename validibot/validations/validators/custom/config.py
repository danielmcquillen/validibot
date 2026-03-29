"""Validator config for the Custom validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="custom-validator",
    name="Custom Validator",
    description="User-defined validator with custom container logic.",
    validation_type=ValidationType.CUSTOM_VALIDATOR,
    validator_class=(
        "validibot.validations.validators.custom.validator.CustomValidator"
    ),
    output_envelope_class=(
        "validibot_shared.validations.envelopes.ValidationOutputEnvelope"
    ),
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
    supports_assertions=True,
)
