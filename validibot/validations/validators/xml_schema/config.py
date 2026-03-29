"""Validator config for the XML Schema validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="xml-validator",
    name="XML Validator",
    description="Validate XML data against XSD, RelaxNG, or DTD schemas.",
    validation_type=ValidationType.XML_SCHEMA,
    validator_class=(
        "validibot.validations.validators.xml_schema.validator.XmlSchemaValidator"
    ),
    version="1.0",
    order=2,
    supported_file_types=[SubmissionFileType.XML],
    supported_data_formats=[SubmissionDataFormat.XML],
    allowed_extensions=["xml", "xsd", "rng", "dtd"],
    icon="bi-filetype-xml",
    card_image="XML_SCHEMA_card_img_small.png",
)
