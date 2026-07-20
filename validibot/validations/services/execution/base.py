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

from validibot.validations.constants import ProviderStatusLookupCapability

if TYPE_CHECKING:
    from validibot_shared.validations.envelopes import ValidationInputEnvelope
    from validibot_shared.validations.envelopes import ValidationOutputEnvelope

    from validibot.submissions.models import Submission
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import Validator
    from validibot.validations.services.file_identity import FileIdentity
    from validibot.validations.services.runners.base import ExecutionStatus
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


class ProviderStatusTemporarilyUnavailableError(RuntimeError):
    """A status-capable provider could not answer a status query right now.

    This is intentionally distinct from an unsupported lookup capability and
    from a provider execution state. Reconciliation may retry it for a bounded
    grace period without inventing a provider failure.
    """


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
        """Validator type (e.g., 'energyplus', 'fmu')."""
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

    error_code: str | None = None
    """Machine-readable code for a launch/execution failure, when the backend
    supplied one (e.g. ``schematron.rules_invalid``). Lets a launch-time failure
    carry the same reserved code a callback-time failure would, instead of
    collapsing to a generic message. ``None`` when no code applies."""

    error_meta: dict | None = None
    """Structured metadata for a failure (e.g. ``{"infra_error": True}``) so a
    launch-time infrastructure failure renders as "we couldn't run the check",
    not a rule failure. ``None`` when no metadata applies."""

    # Metadata
    input_uri: str | None = None
    """URI where input envelope was stored."""

    output_uri: str | None = None
    """URI where output envelope is/will be stored."""

    execution_bundle_uri: str | None = None
    """URI of the execution bundle directory."""

    duration_seconds: float | None = None
    """Execution duration in seconds (if completed)."""

    validator_backend_image_digest: str | None = None
    """Resolved sha256 digest of the validator backend image that ran.

    Trust ADR Phase 5 Session A — sync backends populate this from
    the runner's :class:`ExecutionResult`; async backends persist it
    on the step run directly at launch time and leave this ``None``.
    """

    execution_status: ExecutionStatus | None = None
    """Provider-neutral execution state.

    Callers must use this field, rather than the presence of human-readable
    error text, when deciding whether a completed execution succeeded.
    ``None`` is used for launch responses that do not represent a provider
    status observation.
    """


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
    - `check_status()`: Check execution status (for reconciliation)
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

    @property
    def status_lookup_capability(self) -> ProviderStatusLookupCapability:
        """Declare whether provider state can reconcile a missing callback.

        Backends fail closed to ``UNSUPPORTED`` unless their provider exposes
        a durable, per-execution status resource and the adapter implements it.
        """
        return ProviderStatusLookupCapability.UNSUPPORTED

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
            validator_type: Type of validator (e.g., 'energyplus', 'fmu').

        Returns:
            Full container image name with tag.
        """

    def check_status(self, execution_id: str) -> ExecutionResponse | None:
        """
        Check the current status of an execution.

        Used by reconciliation (e.g., cleanup_stuck_runs) to determine if a
        container is still running, has completed, or has failed. This allows
        recovery of lost callbacks for async backends.

        The default implementation returns ``None`` only for a backend whose
        declared capability is ``UNSUPPORTED``. A backend that declares
        ``SUPPORTED`` must return an explicit provider state or raise
        :class:`ProviderStatusTemporarilyUnavailableError`.

        Args:
            execution_id: Execution identifier from previous execute() call.
                For GCP, this is the Cloud Run execution name.

        Returns:
            ExecutionResponse with current status, or ``None`` when lookup is
            unsupported by this backend.
        """
        return None

    def cancel(self, execution_id: str) -> bool:
        """Request best-effort cancellation of one exact provider execution.

        Backends that do not expose cancellation fail closed. Callers fence the
        logical run before this external side effect, so a false result never
        reopens work that the database has already made terminal.
        """
        return False

    def build_input_envelope(
        self,
        request: ExecutionRequest,
        *,
        callback_url: str | None = None,
        callback_id: str | None = None,
        callback_nonce: str | None = None,
        callback_nonce_commitment: str | None = None,
        execution_bundle_uri: str,
        input_file_uris: dict[str, FileIdentity],
        resource_uri_overrides: dict[str, FileIdentity] | None = None,
    ) -> ValidationInputEnvelope:
        """
        Build the input envelope for a validation.

        This method creates the typed input envelope that will be passed to
        the validator container. Subclasses can override for custom behavior.

        Args:
            request: Execution request.
            callback_url: URL for async callbacks (None for sync backends).
            callback_id: Unique ID for callback deduplication.
            callback_nonce: Per-attempt secret echoed only in the callback.
            callback_nonce_commitment: Public commitment included in canonical
                input-envelope hashing.
            execution_bundle_uri: URI of the execution bundle directory.
            input_file_uris: Map of file role to exact immutable file identity.
            resource_uri_overrides: Optional ``resource_id`` →
                complete materialized identity mapping. Local Docker points
                resources at the per-attempt mount instead of host
                ``MEDIA_ROOT`` paths. Cloud Run leaves this as ``None`` and
                resolves strict identity metadata for the ``gs://`` object.

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
            callback_nonce=callback_nonce,
            callback_nonce_commitment=callback_nonce_commitment,
            execution_bundle_uri=execution_bundle_uri,
            skip_callback=not self.is_async,  # Skip callback for sync backends
            input_file_uris=input_file_uris,
            resource_uri_overrides=resource_uri_overrides,
        )
