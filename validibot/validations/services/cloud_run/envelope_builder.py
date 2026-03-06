"""
Envelope builder for creating typed validation input envelopes.

This module provides functions to build domain-specific input envelopes
(EnergyPlusInputEnvelope, FMUInputEnvelope, etc.) from Django model instances.

Design: Simple factory functions, not classes. Each validator type gets its own
builder function. This keeps the code straightforward and easy to test.
"""

from typing import Protocol

from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope
from validibot_shared.energyplus.envelopes import EnergyPlusInputs
from validibot_shared.fmu.envelopes import FMUInputEnvelope
from validibot_shared.fmu.envelopes import FMUInputs
from validibot_shared.fmu.envelopes import FMUSimulationConfig
from validibot_shared.validations.envelopes import ExecutionContext
from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import OrganizationInfo
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import SupportedMimeType
from validibot_shared.validations.envelopes import ValidationInputEnvelope
from validibot_shared.validations.envelopes import ValidatorInfo
from validibot_shared.validations.envelopes import ValidatorType
from validibot_shared.validations.envelopes import WorkflowInfo

from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ValidationType


class ValidatorLike(Protocol):
    """Protocol for validator-like objects (duck typing for easier testing)."""

    id: str
    validation_type: str
    version: str


def build_energyplus_input_envelope(
    *,
    run_id: str,
    validator: ValidatorLike,
    org_id: str,
    org_name: str,
    workflow_id: str,
    step_id: str,
    step_name: str | None,
    model_file_uri: str,
    resource_files: list[ResourceFileItem],
    callback_url: str,
    callback_id: str | None,
    execution_bundle_uri: str,
    timestep_per_hour: int = 4,
    skip_callback: bool = False,
) -> EnergyPlusInputEnvelope:
    """
    Build an EnergyPlusInputEnvelope from Django validation run data.

    This function creates a fully typed input envelope for EnergyPlus validators.
    It takes Django model data and transforms it into the container input format.

    The validator always returns a fixed set of output signals defined in its
    catalog - users don't need to specify which outputs they want.

    Args:
        run_id: Validation run UUID
        validator: Validator instance (or validator-like object)
        org_id: Organization UUID
        org_name: Organization name (for logging)
        workflow_id: Workflow UUID
        step_id: Workflow step UUID
        step_name: Human-readable step name
        model_file_uri: URI to IDF/epJSON file (gs:// for GCS, file:// for local).
            The file extension determines the envelope metadata: ``.idf`` URIs
            produce ``name="model.idf"`` with ``mime_type=ENERGYPLUS_IDF``;
            ``.epjson`` URIs produce ``name="model.epjson"`` with
            ``mime_type=ENERGYPLUS_EPJSON``.  The runner uses the ``name`` field
            to determine the local filename when downloading, and EnergyPlus
            uses the extension to decide IDF vs epJSON parsing mode.
        resource_files: List of ResourceFileItem objects (weather files, etc.)
        callback_url: Django endpoint to POST results
        callback_id: Unique identifier for idempotent callback processing
        execution_bundle_uri: Directory URI for this run's files
        timestep_per_hour: EnergyPlus timesteps (default: 4).
            NOTE: This value reaches the container envelope but the runner
            does not yet use it to configure the EnergyPlus CLI.  See
            ``idf_checks`` and ``run_simulation`` in EnergyPlusStepConfig
            for other settings that are stored but not yet forwarded.
        skip_callback: If True, container won't POST callback after completion

    Returns:
        Fully populated EnergyPlusInputEnvelope ready for storage upload

    Example:
        >>> envelope = build_energyplus_input_envelope(
        ...     run_id=str(run.id),
        ...     validator=run.validator,
        ...     org_id=str(run.org.id),
        ...     org_name=run.org.name,
        ...     workflow_id=str(run.workflow.id),
        ...     step_id=str(run.step.id),
        ...     step_name=run.step.name,
        ...     model_file_uri="gs://bucket/model.idf",
        ...     resource_files=[ResourceFileItem(id="...", type="energyplus_weather", uri="gs://...")],
        ...     callback_url="https://api.example.com/callbacks/",
        ...     execution_bundle_uri="gs://bucket/runs/abc-123/",
        ...     timestep_per_hour=4,
        ... )
    """
    # Build validator info
    validator_type = ValidatorType(getattr(validator, "validation_type", ""))
    validator_info = ValidatorInfo(
        id=str(validator.id),
        type=validator_type,
        version=validator.version,
    )

    # Build organization info
    org_info = OrganizationInfo(
        id=org_id,
        name=org_name,
    )

    # Build workflow info
    workflow_info = WorkflowInfo(
        id=workflow_id,
        step_id=step_id,
        step_name=step_name,
    )

    # Build input files list (only the model file; weather comes via resource_files).
    # The file extension determines the envelope metadata — the runner uses
    # file_item.name as the local filename, and EnergyPlus uses the extension
    # to decide IDF vs epJSON parsing mode.
    if model_file_uri.lower().endswith(".epjson"):
        model_name = "model.epjson"
        model_mime = SupportedMimeType.ENERGYPLUS_EPJSON
    else:
        model_name = "model.idf"
        model_mime = SupportedMimeType.ENERGYPLUS_IDF

    input_files = [
        InputFileItem(
            name=model_name,
            mime_type=model_mime,
            role="primary-model",
            uri=model_file_uri,
        ),
    ]

    # Build EnergyPlus-specific inputs
    energyplus_inputs = EnergyPlusInputs(
        timestep_per_hour=timestep_per_hour,
    )

    # Build execution context
    execution_context = ExecutionContext(
        callback_id=callback_id,
        callback_url=callback_url,
        execution_bundle_uri=execution_bundle_uri,
        skip_callback=skip_callback,
    )

    # Build the envelope
    envelope = EnergyPlusInputEnvelope(
        run_id=run_id,
        validator=validator_info,
        org=org_info,
        workflow=workflow_info,
        input_files=input_files,
        resource_files=resource_files,
        inputs=energyplus_inputs,
        context=execution_context,
    )

    return envelope


def _resolve_step_resources(
    step,
    *,
    role: str | None = None,
) -> list[ResourceFileItem]:
    """Resolve a step's ``WorkflowStepResource`` rows to ``ResourceFileItem`` objects.

    Queries the relational ``step.step_resources`` reverse relation (FK-backed)
    rather than parsing UUID strings from the JSON config. This provides
    referential integrity: stale or unauthorized references are impossible
    because the FK is PROTECT on ``ValidatorResourceFile``.

    For catalog-reference resources, the ``ResourceFileItem.id`` and ``type``
    come from the underlying ``ValidatorResourceFile``. For step-owned files,
    the ``id`` is the ``WorkflowStepResource.pk`` and ``type`` is the
    ``resource_type`` field on the record itself.

    Args:
        step: WorkflowStep instance with ``step_resources`` relation.
        role: If provided, only return resources matching this role
              (e.g., ``WorkflowStepResource.WEATHER_FILE``).

    Returns:
        List of ``ResourceFileItem`` objects with resolved storage URIs.
    """

    queryset = step.step_resources.select_related("validator_resource_file")
    if role:
        queryset = queryset.filter(role=role)

    items: list[ResourceFileItem] = []
    for sr in queryset:
        if sr.is_catalog_reference:
            vrf = sr.validator_resource_file
            items.append(
                ResourceFileItem(
                    id=str(vrf.id),
                    type=vrf.resource_type,
                    uri=sr.get_storage_uri(),
                )
            )
        else:
            # Step-owned file
            items.append(
                ResourceFileItem(
                    id=str(sr.pk),
                    type=sr.resource_type,
                    uri=sr.get_storage_uri(),
                )
            )
    return items


def build_input_envelope(
    run,  # ValidationRun instance
    callback_url: str,
    callback_id: str | None,
    execution_bundle_uri: str,
    *,
    skip_callback: bool = False,
    input_file_uris: dict[str, str] | None = None,
) -> ValidationInputEnvelope:
    """
    Build the appropriate input envelope based on validator type.

    This is the main entry point for envelope creation. It dispatches to
    type-specific builders based on the current step's validator type.

    Args:
        run: ValidationRun Django model instance
        callback_url: Django callback endpoint URL
        callback_id: Unique identifier for idempotent callback processing
        execution_bundle_uri: Directory URI for this run's files
        skip_callback: If True, container won't POST callback after completion.
            Used for synchronous execution where results are read directly.
        input_file_uris: Optional dict of file role to URI (e.g., {'primary_file_uri': 'file://...'}).
            If provided, these override values from step.config.

    Returns:
        Typed envelope (EnergyPlusInputEnvelope, FMUInputEnvelope, etc.)

    Raises:
        ValueError: If validator type is not supported or no active step run

    Example:
        >>> from validibot.validations.models import ValidationRun
        >>> run = ValidationRun.objects.get(id="abc-123")
        >>> envelope = build_input_envelope(
        ...     run=run,
        ...     callback_url="https://api.example.com/callbacks/",
        ...     callback_id="uuid-for-idempotency",
        ...     execution_bundle_uri="gs://bucket/runs/abc-123/",
        ... )
    """
    # Get the current step run to access validator and step info
    current_step_run = run.current_step_run
    if not current_step_run:
        msg = f"No active step run found for ValidationRun {run.id}"
        raise ValueError(msg)

    step = current_step_run.workflow_step
    validator = step.validator
    if not validator:
        msg = f"WorkflowStep {step.id} has no validator configured"
        raise ValueError(msg)

    # Merge input_file_uris with step.config for lookups
    # input_file_uris takes precedence (they contain dynamically uploaded files)
    step_config = {**(step.config or {}), **(input_file_uris or {})}

    if validator.validation_type == ValidationType.ENERGYPLUS:
        # Get model file URI (primary file from the workflow step or input_file_uris)
        model_file_uri = step_config.get("primary_file_uri")
        if not model_file_uri:
            msg = f"Step {step.id} has no primary_file_uri in config"
            raise ValueError(msg)

        # Resolve resource files from relational WorkflowStepResource rows.
        # Exclude MODEL_TEMPLATE resources — the template is consumed during
        # preprocessing (in Django) and the resolved IDF is uploaded as the
        # primary model file.  Including the template in resource_files would
        # cause the runner to download it unnecessarily, and if the template
        # filename matches the resolved model filename it could overwrite it.
        from validibot.workflows.models import WorkflowStepResource

        resource_files = _resolve_step_resources(
            step, role=WorkflowStepResource.WEATHER_FILE
        )

        # Validate that we have a weather file for EnergyPlus
        has_weather = any(
            rf.type == ResourceFileType.ENERGYPLUS_WEATHER for rf in resource_files
        )
        if not has_weather:
            msg = (
                f"Step {step.id} has no weather file configured"
                " (no WEATHER_FILE step resource)"
            )
            raise ValueError(msg)

        # Get EnergyPlus-specific settings from step config.
        # TODO: Also forward ``idf_checks`` and ``run_simulation`` to the
        #       container once the envelope schema and runner support them.
        #       Currently only ``timestep_per_hour`` reaches the envelope
        #       (and even that is ignored by the runner).
        timestep_per_hour = step_config.get("timestep_per_hour", 4)

        return build_energyplus_input_envelope(
            run_id=str(run.id),
            validator=validator,
            org_id=str(run.org.id),
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            model_file_uri=model_file_uri,
            resource_files=resource_files,
            callback_url=callback_url,
            callback_id=callback_id,
            execution_bundle_uri=execution_bundle_uri,
            timestep_per_hour=timestep_per_hour,
            skip_callback=skip_callback,
        )
    if validator.validation_type == ValidationType.FMU:
        # FMU location: use gcs_uri when present, otherwise local file path
        fmu_model = validator.fmu_model
        if not fmu_model:
            msg = f"Validator {validator.id} has no FMU model attached"
            raise ValueError(msg)
        fmu_uri = fmu_model.gcs_uri or getattr(fmu_model.file, "path", "")
        if not fmu_uri:
            msg = f"FMU model {fmu_model.id} has no storage URI or file path"
            raise ValueError(msg)

        # Resolve inputs keyed by catalog slug from the submission content
        # based on catalog binding paths.
        # Here we only carry the values; the launcher is responsible
        # for resolution.
        fmu_inputs = FMUInputs(
            input_values={},
            simulation=FMUSimulationConfig(),
            output_variables=[],
        )

        input_files = [
            InputFileItem(
                name="model.fmu",
                mime_type=SupportedMimeType.FMU,
                role="fmu",
                uri=fmu_uri,
            )
        ]
        context = ExecutionContext(
            callback_id=callback_id,
            callback_url=callback_url,
            execution_bundle_uri=execution_bundle_uri,
            skip_callback=skip_callback,
        )
        return FMUInputEnvelope(
            run_id=str(run.id),
            validator=ValidatorInfo(
                id=str(validator.id),
                type=ValidatorType(validator.validation_type),
                version=validator.version,
            ),
            org=OrganizationInfo(id=str(run.org.id), name=run.org.name),
            workflow=WorkflowInfo(
                id=str(run.workflow.id),
                step_id=str(step.id),
                step_name=step.name,
            ),
            input_files=input_files,
            inputs=fmu_inputs,
            context=context,
        )

    msg = f"Unsupported validator type: {validator.validation_type}"
    raise ValueError(msg)
