"""
Envelope builder for creating typed validation input envelopes.

This module provides functions to build domain-specific input envelopes
(EnergyPlusInputEnvelope, FMUInputEnvelope, etc.) from Django model instances.

Design: Simple factory functions, not classes. Each validator type gets its own
builder function. This keeps the code straightforward and easy to test.
"""

import json
import logging
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote
from urllib.parse import urlparse

from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope
from validibot_shared.energyplus.envelopes import EnergyPlusInputs
from validibot_shared.fmu.envelopes import FMUInputEnvelope
from validibot_shared.fmu.envelopes import FMUInputs
from validibot_shared.fmu.envelopes import FMUSimulationConfig
from validibot_shared.shacl.envelopes import build_shacl_input_envelope
from validibot_shared.validations.envelopes import ExecutionContext
from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import OrganizationInfo
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import SupportedMimeType
from validibot_shared.validations.envelopes import ValidationInputEnvelope
from validibot_shared.validations.envelopes import ValidatorInfo
from validibot_shared.validations.envelopes import ValidatorType
from validibot_shared.validations.envelopes import WorkflowInfo

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ValidationType

logger = logging.getLogger(__name__)


class ValidatorLike(Protocol):
    """Protocol for validator-like objects (duck typing for easier testing)."""

    id: str
    validation_type: str
    version: int | str


def build_energyplus_input_envelope(
    *,
    run_id: str,
    validator: ValidatorLike,
    org_id: str,
    org_name: str,
    workflow_id: str,
    step_id: str,
    step_name: str | None,
    model_file_uri: str | None,
    resource_files: list[ResourceFileItem],
    callback_url: str,
    callback_id: str | None,
    execution_bundle_uri: str,
    timestep_per_hour: int = 4,
    skip_callback: bool = False,
    input_files: list[InputFileItem] | None = None,
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
        version=str(validator.version),
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

    if input_files is None:
        if not model_file_uri:
            msg = "EnergyPlus envelope requires a model_file_uri or input_files"
            raise ValueError(msg)
        input_files = [
            _build_energyplus_input_file_item("primary_model", model_file_uri),
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


def _build_energyplus_input_file_item(
    port_key: str,
    uri: str,
    *,
    role: str = "primary-model",
) -> InputFileItem:
    """Build an EnergyPlus ``InputFileItem`` from a resolved file-port URI."""

    lowered_uri = uri.lower()
    if lowered_uri.endswith((".epjson", ".json")):
        name = "model.epjson" if role == "primary-model" else _filename_from_uri(uri)
        mime_type = SupportedMimeType.ENERGYPLUS_EPJSON
    elif lowered_uri.endswith(".epw"):
        name = _filename_from_uri(uri) or "weather.epw"
        mime_type = SupportedMimeType.ENERGYPLUS_EPW
    else:
        name = "model.idf" if role == "primary-model" else _filename_from_uri(uri)
        mime_type = SupportedMimeType.ENERGYPLUS_IDF

    return InputFileItem(
        name=name or "input-file",
        mime_type=mime_type,
        role=role,
        port_key=port_key,
        uri=uri,
    )


def _filename_from_uri(uri: str) -> str:
    """Return the final path component from a storage URI."""

    parsed = urlparse(uri)
    path = parsed.path or uri
    return Path(unquote(path)).name


def resolve_step_resources(
    step,
    *,
    role: str | None = None,
    resource_uri_overrides: dict[str, str] | None = None,
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
        resource_uri_overrides: Optional mapping of ``resource_id`` to a
            container-visible URI. When provided and a resource's id is
            in the dict, the override is used instead of
            ``WorkflowStepResource.get_storage_uri()``. This is the
            workspace-aware path used by the local Docker dispatch:
            ``WorkflowStepResource.get_storage_uri()`` returns
            ``MEDIA_ROOT``-rooted host paths that are not visible inside
            the per-run container, so the dispatch layer materialises
            each resource into the workspace and overrides the URI
            here to point at
            ``file:///validibot/input/resources/<filename>``. Cloud Run
            leaves this argument as ``None`` and gets the original
            ``gs://`` URI from the model.

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
            resource_id = str(vrf.id)
            resource_type = vrf.resource_type
        else:
            resource_id = str(sr.pk)
            resource_type = sr.resource_type

        # Workspace-aware override: when the local Docker dispatch
        # materialises the resource into the per-run workspace, it
        # supplies the container-visible URI here so the validator
        # backend resolves to the mounted path rather than the host
        # ``MEDIA_ROOT`` path that lives outside the container's mount
        # namespace.
        if resource_uri_overrides and resource_id in resource_uri_overrides:
            uri = resource_uri_overrides[resource_id]
        else:
            uri = sr.get_storage_uri()

        items.append(
            ResourceFileItem(
                id=resource_id,
                type=resource_type,
                uri=uri,
            )
        )
    return items


def _resolve_energyplus_file_port_items(
    *,
    run,
    step,
    step_config: dict,
    input_file_uris: dict[str, str] | None,
    resource_uri_overrides: dict[str, str] | None,
) -> tuple[list[InputFileItem], list[ResourceFileItem]] | None:
    """Resolve declared EnergyPlus artifact input ports into envelope items.

    Returns ``None`` when the validator has no declared artifact input ports,
    allowing unsynced tests/dev databases to keep using the legacy path.
    """

    from validibot.validations.constants import SignalDirection
    from validibot.validations.constants import StepIOMedium
    from validibot.validations.models import StepInputBinding
    from validibot.validations.models import StepIODefinition

    ports = {
        port.contract_key: port
        for port in StepIODefinition.objects.filter(
            validator_id=step.validator_id,
            direction=SignalDirection.INPUT,
            io_medium=StepIOMedium.ARTIFACT,
        )
    }
    if not ports:
        return None

    bindings = {
        binding.signal_definition.contract_key: binding
        for binding in StepInputBinding.objects.filter(
            workflow_step=step,
            signal_definition__in=ports.values(),
        ).select_related("signal_definition")
    }

    input_files: list[InputFileItem] = []
    resource_files: list[ResourceFileItem] = []
    for contract_key in ("primary_model", "weather_file"):
        port = ports.get(contract_key)
        if port is None:
            continue
        binding = bindings.get(contract_key)
        if binding is None:
            msg = (
                f"Required artifact port '{contract_key}' on step {step.id} "
                "has no StepInputBinding."
            )
            raise ValueError(msg)

        _validate_artifact_port_source_scope(port, binding.source_scope)
        if port.envelope_channel == EnvelopeChannel.RESOURCE_FILES:
            if binding.source_scope == BindingSourceScope.WORKFLOW_RESOURCE:
                resource_files.extend(
                    _resolve_workflow_resource_port(
                        step=step,
                        port=port,
                        binding=binding,
                        resource_uri_overrides=resource_uri_overrides,
                    ),
                )
                continue

            uri = _resolve_artifact_or_submission_file_uri(
                run=run,
                step=step,
                step_config=step_config,
                input_file_uris=input_file_uris,
                port=port,
                binding=binding,
            )
            input_files.append(
                _build_energyplus_input_file_item(
                    port.contract_key,
                    uri,
                    role=port.role or "weather",
                ),
            )
            continue

        uri = _resolve_artifact_or_submission_file_uri(
            run=run,
            step=step,
            step_config=step_config,
            input_file_uris=input_file_uris,
            port=port,
            binding=binding,
        )
        input_files.append(
            _build_energyplus_input_file_item(
                port.contract_key,
                uri,
                role=port.role or "primary-model",
            ),
        )

    return input_files, resource_files


def _validate_artifact_port_source_scope(port, source_scope: str) -> None:
    """Fail closed when a binding uses a source scope the port did not allow."""

    allowed = list(port.allowed_source_scopes or [])
    if allowed and source_scope not in allowed:
        msg = (
            f"Artifact port '{port.contract_key}' does not allow source scope "
            f"'{source_scope}'. Allowed scopes: {', '.join(allowed)}."
        )
        raise ValueError(msg)


def _resolve_workflow_resource_port(
    *,
    step,
    port,
    binding,
    resource_uri_overrides: dict[str, str] | None,
) -> list[ResourceFileItem]:
    """Resolve a workflow-resource artifact port to resource_files items."""

    expected_type = binding.source_data_path or port.resource_type or port.data_format
    resources = resolve_step_resources(
        step,
        resource_uri_overrides=resource_uri_overrides,
    )
    matches = [item for item in resources if item.type == expected_type]
    if not matches:
        msg = (
            f"Required artifact port '{port.contract_key}' could not find "
            f"workflow resource type '{expected_type}' on step {step.id}."
        )
        raise ValueError(msg)
    if port.max_items == 1 and len(matches) > 1:
        msg = (
            f"Artifact port '{port.contract_key}' expected one resource but "
            f"found {len(matches)} matching '{expected_type}' resources."
        )
        raise ValueError(msg)

    for item in matches:
        item.port_key = port.contract_key
    return matches


def _resolve_artifact_or_submission_file_uri(
    *,
    run,
    step,
    step_config: dict,
    input_file_uris: dict[str, str] | None,
    port,
    binding,
) -> str:
    """Resolve submitted-file or upstream-artifact binding to a storage URI."""

    if binding.source_scope == BindingSourceScope.SUBMISSION_FILE:
        return _resolve_submission_file_uri(
            step_config=step_config,
            input_file_uris=input_file_uris,
            port=port,
            binding=binding,
        )

    if binding.source_scope == BindingSourceScope.UPSTREAM_ARTIFACT:
        from validibot.validations.services.path_resolution import resolve_input_signal
        from validibot.validations.services.run_context import RunContextBuilder

        context = RunContextBuilder(run, step).build()
        resolved = resolve_input_signal(
            binding,
            upstream_steps=context.upstream_steps,
        )
        if not resolved.resolved or not isinstance(resolved.value, dict):
            msg = (
                f"Artifact port '{port.contract_key}' could not resolve upstream "
                f"artifact '{binding.source_data_path}'."
            )
            raise ValueError(msg)
        uri = str(resolved.value.get("uri") or "")
        if not uri:
            msg = (
                f"Artifact port '{port.contract_key}' resolved an artifact "
                "without a storage URI."
            )
            raise ValueError(msg)
        return uri

    msg = (
        f"Artifact port '{port.contract_key}' source scope "
        f"'{binding.source_scope}' is not materializable for EnergyPlus yet."
    )
    raise ValueError(msg)


def _resolve_submission_file_uri(
    *,
    step_config: dict,
    input_file_uris: dict[str, str] | None,
    port,
    binding,
) -> str:
    """Resolve a submitted-file port from runtime overrides or step config."""

    candidates = [
        binding.source_data_path,
        port.role,
        port.contract_key,
        f"{port.contract_key}_uri",
    ]
    if port.contract_key == "primary_model":
        candidates.append("primary_file_uri")

    sources = [input_file_uris or {}, step_config or {}]
    for source in sources:
        for key in candidates:
            if key and source.get(key):
                return source[key]

    msg = (
        f"Required artifact port '{port.contract_key}' could not resolve a "
        f"submitted file URI from {', '.join(k for k in candidates if k)}."
    )
    raise ValueError(msg)


def build_input_envelope(
    run,  # ValidationRun instance
    callback_url: str,
    callback_id: str | None,
    execution_bundle_uri: str,
    *,
    skip_callback: bool = False,
    input_file_uris: dict[str, str] | None = None,
    resource_uri_overrides: dict[str, str] | None = None,
) -> ValidationInputEnvelope:
    """
    Build the appropriate input envelope based on validator type.

    This is the main entry point for envelope creation. It dispatches to
    type-specific builders based on the current step's validator type.

    Args:
        run: ValidationRun Django model instance
        callback_url: Django callback endpoint URL
        callback_id: Unique identifier for idempotent callback processing
        execution_bundle_uri: Directory URI for this run's files. For
            the local Docker dispatch path, this is the container path
            (``file:///validibot/output``); for Cloud Run it is the
            per-job ``gs://`` prefix.
        skip_callback: If True, container won't POST callback after completion.
            Used for synchronous execution where results are read directly.
        input_file_uris: Optional dict of file role to URI (e.g.,
            ``{'primary_file_uri': 'file:///validibot/input/model.idf'}``).
            If provided, these override values from ``step.config``. Recognised
            roles: ``primary_file_uri`` (EnergyPlus model file),
            ``fmu_model_uri`` (FMU model file). Used by the local Docker
            dispatch to point input files at the per-run mount path.
        resource_uri_overrides: Optional mapping of ``resource_id`` to
            a container-visible URI for resource files (weather data,
            FMU dependencies, etc.). Used by the local Docker dispatch
            path so resource files in the envelope point at the
            workspace's ``input/resources/`` mount instead of the host
            ``MEDIA_ROOT`` path that the model's ``get_storage_uri()``
            returns by default. Cloud Run leaves this as ``None`` and
            gets the original ``gs://`` URIs.

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

    # Merge both step-config buckets with input_file_uris for runtime lookups.
    # ``config`` holds semantic keys (e.g. timestep_per_hour), ``display_settings``
    # holds cosmetic/runtime-injected keys (ADR-2026-06-18); input_file_uris takes
    # precedence last (it contains the dynamically uploaded primary_file_uri).
    step_config = {
        **(step.config or {}),
        **(step.display_settings or {}),
        **(input_file_uris or {}),
    }

    if validator.validation_type == ValidationType.ENERGYPLUS:
        resolved_file_ports = _resolve_energyplus_file_port_items(
            run=run,
            step=step,
            step_config=step_config,
            input_file_uris=input_file_uris,
            resource_uri_overrides=resource_uri_overrides,
        )
        if resolved_file_ports is not None:
            input_files, resource_files = resolved_file_ports
            if not any(item.port_key == "primary_model" for item in input_files):
                msg = f"Step {step.id} has no primary_model file port resolved"
                raise ValueError(msg)
            if not any(
                item.port_key == "weather_file"
                for item in [*input_files, *resource_files]
            ):
                msg = f"Step {step.id} has no weather_file port resolved"
                raise ValueError(msg)

            timestep_per_hour = step_config.get("timestep_per_hour", 4)
            return build_energyplus_input_envelope(
                run_id=str(run.id),
                validator=validator,
                org_id=str(run.org.id),
                org_name=run.org.name,
                workflow_id=str(run.workflow.id),
                step_id=str(step.id),
                step_name=step.name,
                model_file_uri=None,
                input_files=input_files,
                resource_files=resource_files,
                callback_url=callback_url,
                callback_id=callback_id,
                execution_bundle_uri=execution_bundle_uri,
                timestep_per_hour=timestep_per_hour,
                skip_callback=skip_callback,
            )

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

        resource_files = resolve_step_resources(
            step,
            role=WorkflowStepResource.WEATHER_FILE,
            resource_uri_overrides=resource_uri_overrides,
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
        from validibot.workflows.models import WorkflowStepResource

        # Step-level FMU: check for a step-owned FMU resource first.
        # Falls back to the library validator's FMU model.
        fmu_resource = step.step_resources.filter(
            role=WorkflowStepResource.FMU_MODEL,
        ).first()

        # Workspace-aware override: the local Docker dispatch passes
        # the container-visible URI for the FMU model file via
        # ``input_file_uris["fmu_model_uri"]``. When set, it wins over
        # any model-derived URI. Cloud Run leaves this unset and falls
        # through to the gs:// path.
        overridden_fmu_uri = (input_file_uris or {}).get("fmu_model_uri")

        if fmu_resource:
            # Step-level upload — use get_storage_uri() which returns
            # gs:// in production (GCS) or file:// locally, matching
            # what the container runner expects.
            fmu_uri = overridden_fmu_uri or fmu_resource.get_storage_uri()
            sim_config = (step.config or {}).get("fmu_simulation") or {}
        else:
            # Library validator — existing behavior
            fmu_model = validator.fmu_model
            if not fmu_model:
                msg = f"Validator {validator.id} has no FMU model attached"
                raise ValueError(msg)
            fmu_uri = (
                overridden_fmu_uri
                or fmu_model.gcs_uri
                or getattr(fmu_model.file, "path", "")
            )
            if not fmu_uri:
                msg = f"FMU model {fmu_model.id} has no storage URI or file path"
                raise ValueError(msg)
            sim_config = {}

        # Build simulation config, only overriding fields that have values.
        # The shared FMUSimulationConfig has non-optional defaults for
        # start_time, stop_time, step_size — only pass them if explicitly set.
        sim_kwargs = {}
        for key in ("start_time", "stop_time", "step_size", "tolerance"):
            val = sim_config.get(key)
            if val is not None:
                sim_kwargs[key] = val

        # Resolve FMU input values from explicit StepInputBinding rows only.
        # There is no raw-submission fallback: bindings are the contract that
        # makes input identity, defaults, traces, and cross-step references
        # auditable.
        input_values: dict = {}
        has_bindings = step.signal_bindings.filter(
            signal_definition__direction="input",
        ).exists()

        if has_bindings and current_step_run:
            from validibot.validations.models import ResolvedInputTrace
            from validibot.validations.services.path_resolution import (
                InputSignalResolutionError,
            )
            from validibot.validations.services.path_resolution import (
                resolve_step_input_signals,
            )

            submission_data: dict = {}
            submission_metadata: dict = {}
            if run.submission:
                try:
                    content = run.submission.get_content()
                    if content:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            submission_data = parsed
                except (json.JSONDecodeError, Exception):
                    logger.warning(
                        "Could not parse submission content as JSON for run %s",
                        run.id,
                    )
                # Submission metadata is a JSONField (always a dict),
                # needed for signals scoped to SUBMISSION_METADATA
                # (e.g., EnergyPlus expected_floor_area_m2).
                submission_metadata = run.submission.metadata or {}

            # Canonical upstream values for cross-step resolution. These come
            # from completed step-run records, never from presentation JSON.
            from validibot.validations.services.run_context import RunContextBuilder

            upstream = RunContextBuilder(run, step).build_upstream_steps()

            # Resolve workflow-level signals so SIGNAL-scoped bindings
            # can look up values from the workflow's signal namespace.
            # This intentionally propagates exceptions: if signal
            # resolution fails the step must not proceed with
            # potentially missing input values.
            workflow_signals_dict: dict = {}
            if step.workflow:
                from validibot.validations.services.signal_resolution import (
                    resolve_workflow_signals,
                )

                sig_result = resolve_workflow_signals(
                    step.workflow,
                    submission_data,
                )
                workflow_signals_dict = sig_result.signals

            try:
                input_values, traces = resolve_step_input_signals(
                    step,
                    current_step_run,
                    submission_data=submission_data,
                    submission_metadata=submission_metadata,
                    upstream_steps=upstream,
                    workflow_signals=workflow_signals_dict,
                )
                if traces:
                    ResolvedInputTrace.objects.bulk_create(traces)
            except InputSignalResolutionError as exc:
                # Persist ALL traces (successes + failures) for diagnostics
                # even when resolution fails. The exception carries the
                # complete trace list so operators can see exactly which
                # signals resolved and which didn't.
                if exc.traces:
                    ResolvedInputTrace.objects.bulk_create(exc.traces)
                raise

            # Persist the fully-resolved input values twice, with different
            # keys for different consumers:
            #
            # * ``input_values`` uses Validibot contract keys for downstream
            #   ``steps.<key>.input.*`` access.
            # * ``output["resolved_inputs"]`` preserves native/provider keys
            #   because FMU start values and output-stage assertion payloads
            #   historically use the provider variable names.
            if current_step_run:
                current_step_run.input_values = {
                    trace.signal_contract_key: trace.value_snapshot
                    for trace in traces
                    if trace.resolved
                }
                output = dict(current_step_run.output or {})
                output["resolved_inputs"] = input_values
                current_step_run.output = output
                current_step_run.save(update_fields=["input_values", "output"])
        elif _fmu_step_declares_inputs(step):
            msg = (
                f"Step {step.id} declares FMU input signals but has no "
                "StepInputBinding rows. Configure input bindings before launch."
            )
            raise ValueError(msg)

        # Extract output variable names: prefer StepIODefinition rows,
        # fall back to step config JSON.
        from validibot.validations.constants import SignalDirection
        from validibot.validations.constants import SignalOriginKind
        from validibot.validations.models import StepIODefinition

        output_sigs = StepIODefinition.objects.filter(
            workflow_step=step,
            direction=SignalDirection.OUTPUT,
            origin_kind=SignalOriginKind.FMU,
        )
        output_variables = [sig.native_name for sig in output_sigs]

        fmu_inputs = FMUInputs(
            input_values=input_values,
            simulation=FMUSimulationConfig(**sim_kwargs),
            output_variables=output_variables,
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
                version=str(validator.version),
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

    if validator.validation_type == ValidationType.SHACL:
        # The RDF submission is the primary file. For sync Docker dispatch the
        # workspace materialiser sets ``primary_file_uri`` to the container path;
        # for async Cloud Run, ``launch_shacl_validation`` uploads the submission
        # to GCS and passes its gs:// URI here via ``input_file_uris``.
        submission_uri = step_config.get("primary_file_uri")
        if not submission_uri:
            msg = f"Step {step.id} has no primary_file_uri in config for SHACL"
            raise ValueError(msg)

        # Resolve shapes/ontology/settings/SPARQL-ASK assertions from the DB
        # (the container has none) and ship them in the typed inputs.
        from validibot.validations.validators.shacl.launch import resolve_shacl_inputs

        shacl_inputs = resolve_shacl_inputs(
            validator=validator,
            ruleset=step.ruleset,
            submission=run.submission,
        )
        return build_shacl_input_envelope(
            run_id=str(run.id),
            validator=validator,
            org_id=str(run.org.id),
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            submission_uri=submission_uri,
            inputs=shacl_inputs,
            callback_url=callback_url,
            callback_id=callback_id,
            execution_bundle_uri=execution_bundle_uri,
            skip_callback=skip_callback,
        )

    if validator.validation_type == ValidationType.SCHEMATRON:
        # The XML submission is the primary file; the author's Schematron
        # rules travel INLINE in the typed inputs (ADR-2026-07-01 D4b) —
        # the SHACL shapes_text pattern. ``resolve_schematron_inputs``
        # reads them from the step's ruleset (where the step-config upload
        # stored them); the container compiles and runs them in isolation.
        #
        # Imports are deliberately local: ``validibot_shared.schematron``
        # requires validibot-shared >= 0.12.0, and this branch is the only
        # part of the envelope builder that touches it.
        submission_uri = step_config.get("primary_file_uri")
        if not submission_uri:
            msg = f"Step {step.id} has no primary_file_uri in config for Schematron"
            raise ValueError(msg)

        from validibot_shared.schematron.envelopes import (
            build_schematron_input_envelope,
        )

        from validibot.validations.validators.schematron.launch import (
            resolve_schematron_inputs,
        )

        schematron_inputs = resolve_schematron_inputs(
            validator=validator,
            ruleset=step.ruleset,
        )
        return build_schematron_input_envelope(
            run_id=str(run.id),
            validator=validator,
            org_id=str(run.org.id),
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            submission_uri=submission_uri,
            inputs=schematron_inputs,
            callback_url=callback_url,
            callback_id=callback_id,
            execution_bundle_uri=execution_bundle_uri,
            skip_callback=skip_callback,
        )

    msg = f"Unsupported validator type: {validator.validation_type}"
    raise ValueError(msg)


def _fmu_step_declares_inputs(step) -> bool:
    """Return whether this FMU step has declared input signals.

    Step-owned FMU uploads attach signals to ``workflow_step``. Library FMU
    validators may attach them to the reusable validator. Either form means
    launch requires explicit ``StepInputBinding`` rows.
    """
    from validibot.validations.constants import SignalDirection
    from validibot.validations.constants import SignalOriginKind
    from validibot.validations.models import StepIODefinition

    step_owned_inputs = StepIODefinition.objects.filter(
        workflow_step=step,
        direction=SignalDirection.INPUT,
        origin_kind=SignalOriginKind.FMU,
    ).exists()
    if step_owned_inputs:
        return True

    validator_id = getattr(step, "validator_id", None)
    if validator_id is None:
        return False

    return StepIODefinition.objects.filter(
        validator_id=validator_id,
        direction=SignalDirection.INPUT,
        origin_kind=SignalOriginKind.FMU,
    ).exists()
