"""
Configuration for the FMU system validator.

FMU validators have their catalog entries created dynamically from
the attached FMU via introspection, so this config only defines the
validator metadata itself.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="fmu-validator",
    name="FMU Validation",
    description="Validate and simulate Functional Mock-up Units (FMUs).",
    validation_type=ValidationType.FMU,
    version="1.0",
    order=20,
    has_processor=True,
    processor_name="FMU Simulation",
    is_system=True,
    supported_file_types=[
        SubmissionFileType.BINARY,
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
    ],
    supported_data_formats=[
        SubmissionDataFormat.FMU,
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.TEXT,
    ],
    allowed_extensions=["fmu", "json"],
    icon="bi-cpu",
    card_image="FMU_card_img_small.png",
    catalog_entries=[],
)
