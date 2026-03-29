"""Validator config for the AI Assisted validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="ai-assisted-validator",
    name="AI Assisted Validator",
    description="AI-powered validation using language models.",
    validation_type=ValidationType.AI_ASSIST,
    validator_class=("validibot.validations.validators.ai.validator.AIValidator"),
    version="1.0",
    order=5,
    supported_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
    supported_data_formats=[
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.TEXT,
    ],
    allowed_extensions=["json", "txt"],
    supports_assertions=True,
    icon="bi-robot",
    card_image="AI_ASSIST_card_img_small.png",
)
