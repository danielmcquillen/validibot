"""
Pydantic schemas for validator job execution envelopes.

These schemas define the contract between Django and Cloud Run Job validators.
See docs/adr/2025-12-04-validator-job-interface.md for specification details.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import HttpUrl

# ==============================================================================
# Input Envelope (validibot.input.v1)
# ==============================================================================


class InputKind(str, Enum):
    """Type of input value."""

    FILE = "file"
    JSON = "json"
    STRING = "string"
    NUMBER = "number"


class InputItem(BaseModel):
    """
    An input item for the validator.

    Can be a file (stored in GCS), or an inline value (JSON/string/number).
    """

    name: str = Field(description="Human-readable name of the input")
    kind: InputKind = Field(description="Type of input (file, json, string, number)")
    mime_type: str | None = Field(
        default=None,
        description="MIME type for file inputs (e.g., application/vnd.energyplus.idf)",
    )
    role: str | None = Field(
        default=None,
        description=(
            "Validator-specific role (e.g., 'primary-model', 'weather', 'config')"
        ),
    )
    uri: str | None = Field(
        default=None,
        description="GCS URI for file inputs (e.g., gs://bucket/path/to/file)",
    )
    value: Any | None = Field(
        default=None, description="Inline value for json/string/number inputs"
    )

    model_config = {"extra": "forbid"}


class ValidatorInfo(BaseModel):
    """Information about the validator being executed."""

    id: str = Field(description="Validator UUID")
    type: str = Field(
        description="Validator type (e.g., 'energyplus', 'fmu', 'xml', 'pdf')"
    )
    version: str = Field(description="Validator version (semantic versioning)")

    model_config = {"extra": "forbid"}


class OrganizationInfo(BaseModel):
    """Information about the organization running the validation."""

    id: str = Field(description="Organization UUID")
    name: str = Field(description="Organization name")

    model_config = {"extra": "forbid"}


class WorkflowInfo(BaseModel):
    """Information about the workflow and step being executed."""

    id: str = Field(description="Workflow UUID")
    step_id: str = Field(description="Workflow step UUID")
    step_name: str | None = Field(
        default=None, description="Human-readable step name"
    )

    model_config = {"extra": "forbid"}


class ExecutionContext(BaseModel):
    """Execution context and callback information."""

    callback_url: HttpUrl = Field(
        description="URL to POST callback when validation completes"
    )
    callback_token: str = Field(
        description="JWT token signed with GCP KMS for callback authentication"
    )
    execution_bundle_uri: str = Field(
        description="GCS URI to the execution bundle directory (e.g., gs://bucket/org_id/run_id/)"
    )
    timeout_seconds: int = Field(
        default=3600, description="Maximum execution time in seconds"
    )
    tags: list[str] = Field(default_factory=list, description="Execution tags")

    model_config = {"extra": "forbid"}


class ValidationInputEnvelope(BaseModel):
    """
    Input envelope for validator jobs (validibot.input.v1).

    This is written to GCS as input.json by Django before triggering the job.
    """

    schema_version: Literal["validibot.input.v1"] = "validibot.input.v1"
    run_id: str = Field(description="Unique run identifier (UUID)")
    validator: ValidatorInfo
    org: OrganizationInfo
    workflow: WorkflowInfo
    inputs: list[InputItem] = Field(
        description="List of inputs for the validator (files, config, etc.)"
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Validator-specific configuration (validated by validator container)"
        ),
    )
    context: ExecutionContext

    model_config = {"extra": "forbid"}


# ==============================================================================
# Result Envelope (validibot.result.v1)
# ==============================================================================


class ValidationStatus(str, Enum):
    """Validation result status."""

    SUCCESS = "success"  # Validation completed successfully, no errors
    FAILED_VALIDATION = "failed_validation"  # Validation found errors (user's fault)
    FAILED_RUNTIME = "failed_runtime"  # Runtime error in validator (system fault)
    CANCELLED = "cancelled"  # User or system cancelled the job


class MessageSeverity(str, Enum):
    """Severity level for validation messages."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class MessageLocation(BaseModel):
    """Location information for a validation message."""

    file_role: str | None = Field(
        default=None, description="Input file role (references input.role)"
    )
    line: int | None = Field(default=None, description="Line number")
    column: int | None = Field(default=None, description="Column number")
    path: str | None = Field(
        default=None, description="Object path or XPath-like identifier"
    )

    model_config = {"extra": "forbid"}


class ValidationMessage(BaseModel):
    """A validation finding, warning, or error."""

    severity: MessageSeverity
    code: str | None = Field(
        default=None, description="Error code (e.g., 'EP001', 'FMU_INIT_ERROR')"
    )
    text: str = Field(description="Human-readable message")
    location: MessageLocation | None = Field(
        default=None, description="Location of the issue"
    )
    tags: list[str] = Field(default_factory=list, description="Message tags/categories")

    model_config = {"extra": "forbid"}


class ValidationMetric(BaseModel):
    """A computed metric from the validation/simulation."""

    name: str = Field(description="Metric name (e.g., 'zone_temp_max')")
    value: float | int | str = Field(description="Metric value")
    unit: str | None = Field(default=None, description="Unit (e.g., 'C', 'kWh', 'm2')")
    category: str | None = Field(
        default=None, description="Category (e.g., 'comfort', 'energy', 'performance')"
    )
    tags: list[str] = Field(default_factory=list, description="Metric tags")

    model_config = {"extra": "forbid"}


class ValidationArtifact(BaseModel):
    """A file artifact produced by the validator."""

    name: str = Field(description="Artifact name")
    type: str = Field(
        description=(
            "Artifact type (e.g., 'simulation-db', 'report-html', 'timeseries-csv')"
        )
    )
    mime_type: str | None = Field(
        default=None, description="MIME type (e.g., 'application/x-sqlite3')"
    )
    uri: str = Field(
        description="GCS URI to the artifact (e.g., gs://bucket/org_id/run_id/outputs/simulation.sql)"
    )
    size_bytes: int | None = Field(default=None, description="File size in bytes")

    model_config = {"extra": "forbid"}


class RawOutputs(BaseModel):
    """Information about raw output files."""

    format: Literal["directory", "archive"] = Field(
        description="Format of raw outputs (directory or archive)"
    )
    manifest_uri: str = Field(
        description="GCS URI to the manifest file (e.g., gs://bucket/org_id/run_id/outputs/manifest.json)"
    )

    model_config = {"extra": "forbid"}


class ValidationTiming(BaseModel):
    """Timing information for the validation run."""

    queued_at: datetime | None = Field(
        default=None, description="When the job was queued (ISO8601)"
    )
    started_at: datetime | None = Field(
        default=None, description="When execution started (ISO8601)"
    )
    finished_at: datetime | None = Field(
        default=None, description="When execution finished (ISO8601)"
    )

    model_config = {"extra": "forbid"}


class ValidationResultEnvelope(BaseModel):
    """
    Result envelope for validator jobs (validibot.result.v1).

    This is written to GCS as result.json by the validator container after completion.
    """

    schema_version: Literal["validibot.result.v1"] = "validibot.result.v1"
    run_id: str = Field(description="Unique run identifier (matches input)")
    validator: ValidatorInfo
    status: ValidationStatus
    timing: ValidationTiming
    messages: list[ValidationMessage] = Field(
        default_factory=list, description="Validation findings, warnings, errors"
    )
    metrics: list[ValidationMetric] = Field(
        default_factory=list, description="Computed metrics from the validation"
    )
    artifacts: list[ValidationArtifact] = Field(
        default_factory=list, description="Output files produced by the validator"
    )
    raw_outputs: RawOutputs | None = Field(
        default=None, description="Raw output files information"
    )

    model_config = {"extra": "forbid"}


# ==============================================================================
# Callback Payload
# ==============================================================================


class ValidationCallback(BaseModel):
    """
    Callback payload POSTed to Django after validation completes.

    Minimal payload to avoid duplication - Django loads full result.json from GCS.
    """

    callback_token: str = Field(description="JWT token for authentication")
    run_id: str = Field(description="Run identifier")
    status: ValidationStatus
    result_uri: str = Field(
        description="GCS URI to result.json (e.g., gs://bucket/org_id/run_id/result.json)"
    )

    model_config = {"extra": "forbid"}
