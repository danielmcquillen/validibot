"""
Configuration for the FMU system validator.

Per-variable signals (the FMU's actual input/output variables) are
created dynamically from the attached FMU via introspection in
``services.fmu._persist_variables`` (library FMU validators) and
``services.fmu_signals.sync_step_fmu_signals`` (step-level uploads) —
this static config only defines parser-fact step inputs derived from
``modelDescription.xml``. Per ADR-2026-05-22b Phase 6, these facts
let workflow authors gate dispatch with input-stage assertions like
``i.fmi_version == "2.0"`` or ``i.input_variable_count > 0`` before
paying for simulation compute.

The catalog entries are **derived** from
``services.fmu.PARSER_FACT_SPECS`` rather than hand-written. That
single-source-of-truth pattern (May 2026 review P2 finding) prevents
drift between this catalog and the parser-fact ``StepIODefinition``
rows seeded on per-FMU validators / per-step uploads — adding a new
fact only requires extending ``PARSER_FACT_SPECS``.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import ComputeTier
from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.services.fmu import PARSER_FACT_SPECS
from validibot.validations.services.fmu import FMUParserFactSpec
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig


def _spec_to_catalog_entry(spec: FMUParserFactSpec) -> CatalogEntrySpec:
    """Derive a ``CatalogEntrySpec`` from a ``FMUParserFactSpec``.

    Keeps the catalog and the seeded ``StepIODefinition`` rows in
    lockstep — both paths build from the same spec, so a new field on
    ``FMUParserFactSpec`` propagates to both surfaces without parallel
    edits. The May 2026 review caught that hand-written catalog
    entries diverged from seeded rows (richer descriptions on one
    side, missing units on the other) — keeping a single derivation
    rules that class of bug out.
    """
    return CatalogEntrySpec(
        entry_type=CatalogEntryType.SIGNAL,
        run_stage=CatalogRunStage.INPUT,
        slug=spec.contract_key,
        label=spec.label,
        data_type=spec.data_type,
        description=spec.description,
        binding_config={"source": "parser", "key": spec.contract_key},
        metadata={"units": spec.units} if spec.units else {},
        is_required=False,
        on_missing=spec.on_missing,
        order=spec.order,
        source_kind=SignalSourceKind.INTERNAL,
        is_path_editable=False,
    )


config = ValidatorConfig(
    slug="fmu-validator",
    name="FMU Validation",
    short_description="Run FMUs and assert against inputs and outputs.",
    description="Validate and simulate Functional Mock-up Units (FMUs).",
    validation_type=ValidationType.FMU,
    validator_class="validibot.validations.validators.fmu.validator.FMUValidator",
    output_envelope_class="validibot_shared.fmu.envelopes.FMUOutputEnvelope",
    image_name="validibot-validator-backend-fmu",
    # Version bump to revision 2 per ADR-2026-05-22b Phase 6: seven parser-fact
    # step inputs derived from modelDescription.xml at upload/probe
    # time (model_name, fmi_version, variable counts, has_simulation_defaults).
    # sync_validators refuses semantic drift under the same (slug, version)
    # so the bump is required.
    version=2,
    order=20,
    has_processor=True,
    processor_name="FMU Simulation",
    is_system=True,
    supports_assertions=True,
    compute_tier=ComputeTier.HIGH,
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
    catalog_entries=[_spec_to_catalog_entry(spec) for spec in PARSER_FACT_SPECS],
)
