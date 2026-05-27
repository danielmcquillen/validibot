"""Validator config for the SHACL validator.

This is the single source of truth for the system SHACL validator's
metadata: slug, name, description, supported file types, and the
dotted path to the validator class. The community ``sync_validators``
management command and the runtime registry both consume this config.

Library-level custom SHACL validators (org-owned ``Validator`` rows
with ``is_system=False`` and a populated ``default_ruleset``) reuse
the same engine class and the same ``validation_type`` but are created
through the validator-library UI rather than declared here.

NOTE on translations: ``ValidatorConfig`` and ``CatalogEntrySpec`` are
pydantic BaseModels with strict ``str`` fields. ``gettext_lazy`` proxies
are NOT valid strings to pydantic and raise ``ValidationError`` on
import — crashing app boot. Plain strings are intentional here and match
the convention used by every other validator config. If translation is
needed for these labels in the future, translate at the render site
(template ``{% trans %}`` or view-layer ``_()``), not at the config
definition.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="shacl-validator",
    name="SHACL Validator",
    short_description=(
        "Validate RDF graphs against SHACL shapes to check required "
        "classes, properties, and semantic constraints."
    ),
    description=(
        "Validate RDF graphs (Turtle, JSON-LD, RDF/XML) against SHACL "
        "shapes. Common configurations include ASHRAE 223P, Guideline "
        "36, Brick Schema, Project Haystack 4, and project-specific "
        "shapes."
    ),
    validation_type=ValidationType.SHACL,
    validator_class=("validibot.validations.validators.shacl.validator.SHACLValidator"),
    version=3,
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
    catalog_entries=[
        CatalogEntrySpec(
            slug="parse_ok",
            label="Parse OK",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether the submitted RDF parsed successfully.",
            order=10,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="parse_serialization",
            label="Parse Serialization",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            description="RDF serialization used by the SHACL parser.",
            order=20,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="triple_count",
            label="Triple Count",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of triples in the submitted RDF graph.",
            order=30,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="namespaces_present",
            label="Namespaces Present",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.OBJECT,
            description="Namespace URI list seen in the submitted RDF graph.",
            order=40,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="has_s223_namespace",
            label="Has ASHRAE 223P Namespace",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether the graph uses the ASHRAE 223P namespace.",
            order=50,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="has_g36_namespace",
            label="Has Guideline 36 Namespace",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether the graph uses the Guideline 36 namespace.",
            order=60,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="has_brick_namespace",
            label="Has Brick Namespace",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description="Whether the graph uses the Brick namespace.",
            order=70,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="shacl_violation_count",
            label="SHACL Violation Count",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of SHACL violation results.",
            order=80,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="shacl_warning_count",
            label="SHACL Warning Count",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of SHACL warning results.",
            order=90,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="shacl_info_count",
            label="SHACL Info Count",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of SHACL info results.",
            order=100,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="shacl_total_count",
            label="SHACL Total Result Count",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Total number of SHACL results at all severities.",
            order=110,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
    ],
    icon="bi-diagram-3",
    card_image="default_card_img_small.png",
)
