"""
Base class for execution backends.

This module defines the abstract interface that all execution backends must implement.
The interface is designed to support both synchronous (Docker) and asynchronous
(Cloud Run, AWS Batch) execution models.

## Design Principles

1. **Encapsulate full lifecycle**: Backends handle storage, container execution,
   and result retrieval. Callers don't need to know implementation details.

2. **Clear async vs sync distinction**: The `is_async` property tells callers
   whether to expect immediate results or callback-based completion.

3. **Storage-agnostic**: Each backend uses its own storage (local filesystem,
   GCS, S3) internally. The interface works with envelope objects, not URIs.

4. **Idempotency support**: Backends should handle retry scenarios gracefully.
"""

from __future__ import annotations

import logging
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot_shared.validations.envelopes import ValidationInputEnvelope
    from validibot_shared.validations.envelopes import ValidationOutputEnvelope

    from validibot.submissions.models import Submission
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import Validator
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


@dataclass
class ExecutionRequest:
    """
    Request to execute an advanced validation.

    Contains all the information needed to execute a validation, regardless
    of which backend is used.
    """

    run: ValidationRun
    validator: Validator
    submission: Submission
    step: WorkflowStep

    @property
    def run_id(self) -> str:
        """Unique identifier for this validation run."""
        return str(self.run.id)

    @property
    def org_id(self) -> str:
        """Organization ID for storage path construction."""
        return str(self.run.org.id)

    @property
    def validator_type(self) -> str:
        """Validator type (e.g., 'energyplus', 'fmi')."""
        return self.validator.validation_type.lower()


@dataclass
class ExecutionResponse:
    """
    Response from executing an advanced validation.

    For synchronous backends, contains the full output envelope.
    For asynchronous backends, contains execution tracking info.
    """

    # Execution tracking
    execution_id: str
    """Unique identifier for this execution (container ID, job name, etc.)."""

    is_complete: bool
    """Whether execution has completed (True for sync, False for async pending)."""

    # Result (only populated for sync backends or completed async)
    output_envelope: ValidationOutputEnvelope | None = None
    """Full output envelope if execution completed."""

    # Error info (if execution failed)
    error_message: str | None = None
    """Error message if execution failed."""

    # Metadata
    input_uri: str | None = None
    """URI where input envelope was stored."""

    output_uri: str | None = None
    """URI where output envelope is/will be stored."""

    execution_bundle_uri: str | None = None
    """URI of the execution bundle directory."""

    duration_seconds: float | None = None
    """Execution duration in seconds (if completed)."""


class ExecutionBackend(ABC):
    """
    Abstract base class for validator execution backends.

    An execution backend handles the full lifecycle of running an advanced
    validator container:

    1. Upload input envelope and files to storage
    2. Trigger container execution
    3. (For sync) Wait for completion and download results
    4. Return execution response

    ## Implementation Notes

    Subclasses must implement:
    - `is_async`: Property indicating execution model
    - `execute()`: Main execution method
    - `get_container_image()`: Map validator type to container image

    Subclasses may override:
    - `is_available()`: Check if backend is ready
    - `get_execution_status()`: Poll async execution status
    """

    @property
    @abstractmethod
    def is_async(self) -> bool:
        """
        Whether this backend uses asynchronous execution with callbacks.

        Returns:
            True if execution is async (results via callback).
            False if execution is sync (results returned immediately).
        """

    @property
    def backend_name(self) -> str:
        """Human-readable name for this backend."""
        return self.__class__.__name__

    def is_available(self) -> bool:
        """
        Check if this backend is available and ready for use.

        Returns:
            True if the backend can execute validations.
        """
        return True

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute an advanced validation.

        This is the main entry point for running a validation. The method:
        1. Builds and uploads the input envelope
        2. Triggers container execution
        3. For sync backends: waits for completion and downloads results
        4. Returns execution response

        Args:
            request: Execution request containing run, validator, submission, step.

        Returns:
            ExecutionResponse with execution status and optionally the output.

        Raises:
            RuntimeError: If backend is not available.
            ValueError: If request is invalid.
            TimeoutError: If execution times out (sync backends).
        """

    @abstractmethod
    def get_container_image(self, validator_type: str) -> str:
        """
        Get the container image for a validator type.

        Args:
            validator_type: Type of validator (e.g., 'energyplus', 'fmi').

        Returns:
            Full container image name with tag.
        """

    def get_execution_status(self, execution_id: str) -> ExecutionResponse | None:
        """
        Get the current status of an async execution.

        Only applicable for async backends. Returns None for sync backends.

        Args:
            execution_id: Execution identifier from previous execute() call.

        Returns:
            ExecutionResponse with current status, or None if not supported.
        """
        return None

    def build_input_envelope(
        self,
        request: ExecutionRequest,
        *,
        callback_url: str | None = None,
        callback_id: str | None = None,
        execution_bundle_uri: str,
        input_file_uris: dict[str, str],
    ) -> ValidationInputEnvelope:
        """
        Build the input envelope for a validation.

        This method creates the typed input envelope that will be passed to
        the validator container. Subclasses can override for custom behavior.

        Args:
            request: Execution request.
            callback_url: URL for async callbacks (None for sync backends).
            callback_id: Unique ID for callback deduplication.
            execution_bundle_uri: URI of the execution bundle directory.
            input_file_uris: Map of file role to URI (e.g., {'primary-model': 'file://...'}).

        Returns:
            Typed input envelope (e.g., EnergyPlusInputEnvelope).
        """
        from validibot.validations.services.cloud_run.envelope_builder import (
            build_input_envelope,
        )

        return build_input_envelope(
            run=request.run,
            callback_url=callback_url or "",
            callback_id=callback_id or "",
            execution_bundle_uri=execution_bundle_uri,
            skip_callback=not self.is_async,  # Skip callback for sync backends
            input_file_uris=input_file_uris,
        )
