"""Validator config for the SHACL validator.

This is the single source of truth for the system SHACL validator's
metadata: slug, name, description, supported file types, and the
dotted path to the validator class. The community ``sync_validators``
management command and the runtime registry both consume this config.

Library-level custom SHACL validators (org-owned ``Validator`` rows
with ``is_system=False`` and a populated ``default_ruleset``) reuse
the same engine class and the same ``validation_type`` but are created
through the validator-library UI rather than declared here.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="shacl-validator",
    name="SHACL Validator",
    description=(
        "Validate RDF graphs (Turtle, JSON-LD, RDF/XML) against SHACL "
        "shapes. Common configurations include ASHRAE 223P, Guideline "
        "36, Brick Schema, Project Haystack 4, and project-specific "
        "shapes."
    ),
    validation_type=ValidationType.SHACL,
    validator_class=("validibot.validations.validators.shacl.validator.SHACLValidator"),
    version="0.1",
    order=4,
    supported_file_types=[
        SubmissionFileType.TEXT,
        SubmissionFileType.JSON,
        SubmissionFileType.XML,
    ],
    supported_data_formats=[
        SubmissionDataFormat.TEXT,
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.XML,
    ],
    allowed_extensions=["ttl", "rdf", "jsonld", "nt", "nq"],
    supports_assertions=True,
    icon="bi-diagram-3",
    card_image="default_card_img_small.png",
)
