"""Validator config for the Basic validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="basic-validator",
    name="Basic Validator",
    short_description=(
        "The simplest validator. Lets workflow authors map workflow signals "
        "and add assertions without a validator-specific step I/O catalog."
    ),
    description=(
        "The simplest validator. Lets workflow authors map workflow signals"
        " and add assertions without a validator-specific step I/O catalog."
    ),
    validation_type=ValidationType.BASIC,
    validator_class=("validibot.validations.validators.basic.validator.BasicValidator"),
    version=1,
    order=0,
    supported_file_types=[
        SubmissionFileType.JSON,
        SubmissionFileType.XML,
    ],
    supported_data_formats=[
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.XML,
    ],
    allowed_extensions=["json", "xml"],
    supports_assertions=True,
    icon="bi-journal-bookmark",
    card_image="BASIC_card_img_small.png",
)
