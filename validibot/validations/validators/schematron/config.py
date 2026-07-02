"""Validator config for the Schematron validator.

This is the single source of truth for the system Schematron validator's
metadata: slug, name, description, supported file types, and the dotted path
to the validator class. The community ``sync_validators`` management command
and the runtime registry both consume this config. Creating this module is
what makes the validator discoverable — ``discover_configs()`` scans
validator sub-packages via ``pkgutil`` and imports each ``config.py`` (no
``validators/__init__.py`` edit required, per ADR-2026-07-01 D2).

Schematron is an **advanced/container-routed** validator (SHACL posture):
Saxon + rule-pack XSLT run only in the isolated
``validibot-validator-backend-schematron`` container, while the compute tier
stays LOW — isolation is a safety posture, not a price change (D4).

NOTE on translations: ``ValidatorConfig`` / ``CatalogEntrySpec`` are pydantic
models with strict ``str`` fields — ``gettext_lazy`` proxies crash app boot.
Plain strings are intentional, matching every other validator config.

TODO(shared-0.11.0): declare the container output contract once
``validibot-shared`` >= 0.11.0 (which adds ``validibot_shared.schematron``)
is released and synced into this repo::

    output_envelope_class=(
        "validibot_shared.schematron.envelopes.SchematronOutputEnvelope"
    ),

It is deliberately absent right now: ``register_validator_config()`` resolves
the dotted path eagerly at app boot and raises ImportError when the module
doesn't exist — declaring it before the shared release lands would break
every Django startup. The callback path hard-fails without it, so this MUST
be flipped before (or with) the Phase 3 backend wiring.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ComputeTier
from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="schematron-validator",
    name="Schematron Validator",
    short_description=(
        "Run a curated Schematron rule pack (e.g. EN 16931, Peppol BIS "
        "Billing 3.0) against an XML submission and report failed rules "
        "by their native IDs."
    ),
    description=(
        "Validate XML documents against curated, version-pinned Schematron "
        "rule packs — the publishers' own business rules, preserving native "
        "rule identifiers like BR-CO-15 and PEPPOL-EN16931-R010. Pairs with "
        "an XML Schema step for a complete structural + business-rule "
        "pre-flight. This is a pre-flight developer aid, not a certification "
        "of compliance."
    ),
    validation_type=ValidationType.SCHEMATRON,
    validator_class=(
        "validibot.validations.validators.schematron.validator.SchematronValidator"
    ),
    # Cloud Run Job / Docker image. Set explicitly because the slug
    # ("schematron-validator") would otherwise produce the wrong convention
    # name ("validibot-validator-backend-schematron-validator").
    image_name="validibot-validator-backend-schematron",
    has_processor=True,
    processor_name="Schematron Validation",
    version=1,
    order=3,
    supported_file_types=[SubmissionFileType.XML],
    supported_data_formats=[SubmissionDataFormat.XML],
    allowed_extensions=["xml"],
    supports_assertions=True,
    # Routed to a container for isolation (Saxon/XSLT over untrusted XML),
    # not for heavy compute — metered by launch count (ADR-2026-07-01 D4).
    compute_tier=ComputeTier.LOW,
    icon="bi-card-checklist",
    card_image="default_card_img_small.png",
    # All OUTPUT signals, populated from the container's SVRL summary
    # (INTERNAL source, non-editable path) — same shape as the SHACL config.
    catalog_entries=[
        CatalogEntrySpec(
            slug="passed",
            label="Passed",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.BOOLEAN,
            description=(
                "Whether the run produced zero ERROR-level findings. Null "
                "(unknown) when the engine could not run the rules."
            ),
            order=10,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="error_count",
            label="Error Count",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of ERROR-level Schematron findings.",
            order=20,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="warning_count",
            label="Warning Count",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Number of WARNING-level Schematron findings.",
            order=30,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # `fired_rule_count`, NOT an "assertion count": svrl:fired-rule marks
        # a rule/context the engine evaluated, not an assertion that fired
        # (ADR-2026-07-01 D3 SVRL note).
        CatalogEntrySpec(
            slug="fired_rule_count",
            label="Fired Rule Count",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description=(
                "Number of Schematron rules/contexts the engine evaluated "
                "(svrl:fired-rule elements) — not a count of failed "
                "assertions."
            ),
            order=40,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # A MAP of {rule_id: severity}, e.g. {"BR-CO-15": "ERROR"} — pinned
        # so CEL `"BR-CO-15" in o.finding_rule_ids_by_severity` is key
        # membership and severity is queryable (ADR-2026-07-01 D2). Named
        # "finding_*" because svrl:successful-report entries are active
        # findings too, not just failed asserts.
        CatalogEntrySpec(
            slug="finding_rule_ids_by_severity",
            label="Finding Rule IDs by Severity",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.OBJECT,
            description=(
                "Map of native rule id to resolved severity for every "
                'active finding, e.g. {"BR-CO-15": "ERROR"}. Supports CEL '
                "membership tests and severity-aware gates."
            ),
            order=50,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ── Provenance of the exact executed artefact (D5) ──
        CatalogEntrySpec(
            slug="pack_id",
            label="Pack ID",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            description="Identifier of the executed rule pack.",
            order=60,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="pack_version",
            label="Pack Version",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            description="Pinned version of the executed rule pack.",
            order=70,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="query_binding",
            label="Query Binding",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            description="Schematron query binding of the pack (xslt1/xslt2).",
            order=80,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            slug="engine",
            label="Engine",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.STRING,
            description=(
                "XSLT engine (name + version) that executed the pack, "
                "e.g. 'SaxonC-HE 12.5'."
            ),
            order=90,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
    ],
)
