"""Validator config for the XML Schema validator."""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="xml-validator",
    name="XML Validator",
    short_description=(
        "Validate XML submissions against a XSD, DTD, or RelaxNG "
        "schema provided by the workflow author."
    ),
    description="Validate XML data against XSD, RelaxNG, or DTD schemas.",
    validation_type=ValidationType.XML_SCHEMA,
    validator_class=(
        "validibot.validations.validators.xml_schema.validator.XmlSchemaValidator"
    ),
    version="1.1",
    order=2,
    supported_file_types=[SubmissionFileType.XML],
    supported_data_formats=[SubmissionDataFormat.XML],
    allowed_extensions=["xml", "xsd", "rng", "dtd"],
    supports_assertions=True,
    icon="bi-filetype-xml",
    card_image="XML_SCHEMA_card_img_small.png",
)
