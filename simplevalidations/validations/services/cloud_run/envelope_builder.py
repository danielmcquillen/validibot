"""
Envelope builder for creating typed validation input envelopes.

This module provides functions to build domain-specific input envelopes
(EnergyPlusInputEnvelope, FMIInputEnvelope, etc.) from Django model instances.

Design: Simple factory functions, not classes. Each validator type gets its own
builder function. This keeps the code straightforward and easy to test.
"""

from typing import Protocol

from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope
from sv_shared.energyplus.envelopes import EnergyPlusInputs
from sv_shared.validations.envelopes import ExecutionContext
from sv_shared.validations.envelopes import InputFileItem
from sv_shared.validations.envelopes import OrganizationInfo
from sv_shared.validations.envelopes import SupportedMimeType
from sv_shared.validations.envelopes import ValidationInputEnvelope
from sv_shared.validations.envelopes import ValidatorInfo
from sv_shared.validations.envelopes import WorkflowInfo


class ValidatorLike(Protocol):
    """Protocol for validator-like objects (duck typing for easier testing)."""

    id: str
    type: str
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
    weather_file_uri: str,
    callback_url: str,
    callback_token: str,
    execution_bundle_uri: str,
    timestep_per_hour: int = 4,
    output_variables: list[str] | None = None,
) -> EnergyPlusInputEnvelope:
    """
    Build an EnergyPlusInputEnvelope from Django validation run data.

    This function creates a fully typed input envelope for EnergyPlus validators.
    It takes Django model data and transforms it into the Cloud Run Job input format.

    Args:
        run_id: Validation run UUID
        validator: Validator instance (or validator-like object)
        org_id: Organization UUID
        org_name: Organization name (for logging)
        workflow_id: Workflow UUID
        step_id: Workflow step UUID
        step_name: Human-readable step name
        model_file_uri: GCS URI to IDF/epJSON file
        weather_file_uri: GCS URI to EPW weather file
        callback_url: Django endpoint to POST results
        callback_token: JWT token for callback authentication
        execution_bundle_uri: GCS directory for this run's files
        timestep_per_hour: EnergyPlus timesteps (default: 4)
        output_variables: EnergyPlus output variables to collect

    Returns:
        Fully populated EnergyPlusInputEnvelope ready for GCS upload

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
        ...     weather_file_uri="gs://bucket/weather.epw",
        ...     callback_url="https://api.example.com/callbacks/",
        ...     callback_token="jwt_token_here",
        ...     execution_bundle_uri="gs://bucket/runs/abc-123/",
        ...     timestep_per_hour=4,
        ...     output_variables=["Zone Mean Air Temperature"],
        ... )
    """
    # Build validator info
    validator_info = ValidatorInfo(
        id=str(validator.id),
        type=validator.type,
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

    # Build input files list
    input_files = [
        InputFileItem(
            name="model.idf",
            mime_type=SupportedMimeType.ENERGYPLUS_IDF,
            role="primary-model",
            uri=model_file_uri,
        ),
        InputFileItem(
            name="weather.epw",
            mime_type=SupportedMimeType.ENERGYPLUS_EPW,
            role="weather",
            uri=weather_file_uri,
        ),
    ]

    # Build EnergyPlus-specific inputs
    energyplus_inputs = EnergyPlusInputs(
        timestep_per_hour=timestep_per_hour,
        output_variables=output_variables or [],
    )

    # Build execution context
    execution_context = ExecutionContext(
        callback_url=callback_url,
        callback_token=callback_token,
        execution_bundle_uri=execution_bundle_uri,
    )

    # Build the envelope
    envelope = EnergyPlusInputEnvelope(
        run_id=run_id,
        validator=validator_info,
        org=org_info,
        workflow=workflow_info,
        input_files=input_files,
        inputs=energyplus_inputs,
        context=execution_context,
    )

    return envelope


def build_input_envelope(
    run,  # ValidationRun instance
    callback_url: str,
    callback_token: str,
    execution_bundle_uri: str,
) -> ValidationInputEnvelope:
    """
    Build the appropriate input envelope based on validator type.

    This is the main entry point for envelope creation. It dispatches to
    type-specific builders based on run.validator.type.

    Args:
        run: ValidationRun Django model instance
        callback_url: Django callback endpoint URL
        callback_token: JWT token for callback authentication
        execution_bundle_uri: GCS directory for this run's files

    Returns:
        Typed envelope (EnergyPlusInputEnvelope, FMIInputEnvelope, etc.)

    Raises:
        ValueError: If validator type is not supported

    Example:
        >>> from simplevalidations.validations.models import ValidationRun
        >>> run = ValidationRun.objects.get(id="abc-123")
        >>> envelope = build_input_envelope(
        ...     run=run,
        ...     callback_url="https://api.example.com/callbacks/",
        ...     callback_token="jwt_token_here",
        ...     execution_bundle_uri="gs://bucket/runs/abc-123/",
        ... )
    """
    if run.validator.type == "energyplus":
        # Get model file URI (primary file from the workflow step)
        model_file_uri = run.step.primary_file_uri
        if not model_file_uri:
            msg = f"Step {run.step.id} has no primary_file_uri"
            raise ValueError(msg)

        # Get weather file URI (from step configuration or validator config)
        # TODO: Need to determine where weather file is stored
        # For now, assume it's in step.config or validator.config
        weather_file_uri = run.step.config.get("weather_file_uri")
        if not weather_file_uri:
            msg = f"Step {run.step.id} has no weather_file_uri in config"
            raise ValueError(msg)

        # Get EnergyPlus-specific settings
        timestep_per_hour = run.validator.config.get("timestep_per_hour", 4)
        output_variables = run.validator.config.get("output_variables", [])

        return build_energyplus_input_envelope(
            run_id=str(run.id),
            validator=run.validator,
            org_id=str(run.org.id),
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(run.step.id),
            step_name=run.step.name,
            model_file_uri=model_file_uri,
            weather_file_uri=weather_file_uri,
            callback_url=callback_url,
            callback_token=callback_token,
            execution_bundle_uri=execution_bundle_uri,
            timestep_per_hour=timestep_per_hour,
            output_variables=output_variables,
        )
    msg = f"Unsupported validator type: {run.validator.type}"
    raise ValueError(msg)
