"""
Docker Compose execution backend.

This backend runs validator containers locally via the Docker socket. Execution
is synchronous - the worker blocks until the container completes, then reads
the output envelope directly from local storage.

## Execution Flow

```
1. Upload input envelope to local storage (file://)
2. Spawn Docker container with input/output URIs
3. Wait for container to complete (blocking)
4. Read output envelope from local storage
5. Return complete ExecutionResponse
```

## When to Use

Use this backend for:
- Docker Compose deployments (VPS, DigitalOcean, on-premise)
- Local development and testing
- Single-server setups without cloud infrastructure

## Configuration

Settings:
- `VALIDATOR_RUNNER = "docker"`
- `VALIDATOR_RUNNER_OPTIONS` for memory/cpu/timeout limits
- `DATA_STORAGE_ROOT` for local file storage
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

from django.conf import settings

from validibot.core.storage import get_data_storage
from validibot.validations.services.execution.base import ExecutionBackend
from validibot.validations.services.execution.base import ExecutionRequest
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.runners import get_validator_runner

if TYPE_CHECKING:
    from validibot_shared.validations.envelopes import ValidationOutputEnvelope

logger = logging.getLogger(__name__)


class DockerComposeExecutionBackend(ExecutionBackend):
    """
    Docker Compose execution backend using local Docker containers.

    This backend provides synchronous execution of validator containers via
    the local Docker daemon. Results are returned immediately after the
    container completes.

    ## Thread Safety

    This backend is thread-safe. Multiple workers can execute validations
    concurrently - each gets its own isolated storage path and container.

    ## Error Handling

    Container failures are captured and returned in the ExecutionResponse.
    The backend does not raise exceptions for container failures; instead,
    it sets `error_message` in the response.
    """

    def __init__(self) -> None:
        """Initialize the Docker Compose backend."""
        self._storage = None
        self._runner = None

    @property
    def is_async(self) -> bool:
        """Docker Compose execution is synchronous."""
        return False

    @property
    def storage(self):
        """Lazy-load storage backend."""
        if self._storage is None:
            self._storage = get_data_storage()
        return self._storage

    @property
    def runner(self):
        """Lazy-load Docker runner."""
        if self._runner is None:
            self._runner = get_validator_runner()
        return self._runner

    def is_available(self) -> bool:
        """Check if Docker is available."""
        try:
            return self.runner.is_available()
        except Exception:
            logger.exception("Failed to check Docker availability")
            return False

    def get_container_image(self, validator_type: str) -> str:
        """
        Get the container image for a validator type.

        Uses settings to determine the image name and tag.
        """
        # Normalize validator type
        vtype = validator_type.lower()

        # Check for explicit image mapping in settings
        image_map = getattr(settings, "VALIDATOR_IMAGES", {})
        if vtype in image_map:
            return image_map[vtype]

        # Default naming convention
        image_name = f"validibot-validator-{vtype}"
        image_tag = getattr(settings, "VALIDATOR_IMAGE_TAG", "latest")
        registry = getattr(settings, "VALIDATOR_IMAGE_REGISTRY", "")

        if registry:
            return f"{registry}/{image_name}:{image_tag}"
        return f"{image_name}:{image_tag}"

    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute a validation synchronously via Docker.

        This method:
        1. Uploads the submission and input envelope to local storage
        2. Runs the validator container (blocking until completion)
        3. Reads the output envelope from local storage
        4. Returns complete ExecutionResponse

        Args:
            request: Execution request with run, validator, submission, step.

        Returns:
            ExecutionResponse with output_envelope populated if successful.
        """
        if not self.is_available():
            return ExecutionResponse(
                execution_id="",
                is_complete=True,
                error_message="Docker runner is not available",
            )

        # Build storage paths
        base_path = f"runs/{request.org_id}/{request.run_id}"
        input_envelope_path = f"{base_path}/input.json"
        output_envelope_path = f"{base_path}/output.json"

        # Generate a unique execution ID
        execution_id = str(uuid.uuid4())[:12]

        try:
            # 1. Upload submission file(s)
            input_file_uris = self._upload_submission(request, base_path)

            # 2. Build execution bundle URI
            execution_bundle_uri = self.storage.get_uri(base_path)
            input_envelope_uri = self.storage.get_uri(input_envelope_path)
            output_envelope_uri = self.storage.get_uri(output_envelope_path)

            # 3. Build callback URL and ID (unused for sync, but needed for envelope)
            callback_url = self._get_callback_url()
            callback_id = f"step-run-{request.run.current_step_run.id}"

            # 4. Build and upload input envelope
            envelope = self.build_input_envelope(
                request,
                callback_url=callback_url,
                callback_id=callback_id,
                execution_bundle_uri=execution_bundle_uri,
                input_file_uris=input_file_uris,
            )
            self.storage.write_envelope(input_envelope_path, envelope)

            logger.info(
                "Uploaded input envelope for run %s to %s",
                request.run_id,
                input_envelope_uri,
            )

            # 5. Get container image
            container_image = self.get_container_image(request.validator_type)

            logger.info(
                "Executing container: image=%s, input=%s, output=%s",
                container_image,
                input_envelope_uri,
                output_envelope_uri,
            )

            # 6. Run container (blocking)
            result = self.runner.run(
                container_image=container_image,
                input_uri=input_envelope_uri,
                output_uri=output_envelope_uri,
                run_id=request.run_id,
                validator_slug=request.validator_type.lower(),
            )

            # 7. Process result
            if not result.succeeded:
                logger.warning(
                    "Container failed for run %s: exit_code=%d, error=%s",
                    request.run_id,
                    result.exit_code,
                    result.error_message,
                )
                return ExecutionResponse(
                    execution_id=result.execution_id or execution_id,
                    is_complete=True,
                    error_message=result.error_message
                    or f"Container exited with code {result.exit_code}",
                    input_uri=input_envelope_uri,
                    output_uri=output_envelope_uri,
                    execution_bundle_uri=execution_bundle_uri,
                    duration_seconds=result.duration_seconds,
                )

            # 8. Read output envelope
            output_envelope = self._read_output_envelope(output_envelope_path)

            logger.info(
                "Container execution completed for run %s in %.2fs",
                request.run_id,
                result.duration_seconds,
            )

            return ExecutionResponse(
                execution_id=result.execution_id or execution_id,
                is_complete=True,
                output_envelope=output_envelope,
                input_uri=input_envelope_uri,
                output_uri=output_envelope_uri,
                execution_bundle_uri=execution_bundle_uri,
                duration_seconds=result.duration_seconds,
            )

        except TimeoutError as e:
            logger.exception("Container execution timed out for run %s", request.run_id)
            return ExecutionResponse(
                execution_id=execution_id,
                is_complete=True,
                error_message=f"Execution timed out: {e}",
            )

        except Exception as e:
            logger.exception("Failed to execute validation for run %s", request.run_id)
            return ExecutionResponse(
                execution_id=execution_id,
                is_complete=True,
                error_message=f"Execution failed: {e}",
            )

    def _upload_submission(
        self,
        request: ExecutionRequest,
        base_path: str,
    ) -> dict[str, str]:
        """
        Upload submission files to storage.

        Returns a dict mapping file roles to URIs for the input envelope.
        """
        input_file_uris = {}

        # Upload primary submission file
        submission_content = request.submission.get_content()
        if isinstance(submission_content, str):
            submission_bytes = submission_content.encode("utf-8")
        else:
            submission_bytes = submission_content

        # Determine file extension from submission
        submission_filename = request.submission.original_filename or "submission"
        submission_path = f"{base_path}/{submission_filename}"
        self.storage.write(submission_path, submission_bytes)
        input_file_uris["primary_file_uri"] = self.storage.get_uri(submission_path)

        # Check for weather file in step config (for EnergyPlus)
        step_config = request.step.config or {}
        if "weather_file_id" in step_config:
            # Weather file is stored separately - get its URI
            # For now, assume weather files are already in storage
            # TODO: Handle weather file upload properly
            weather_uri = step_config.get("weather_file_uri")
            if weather_uri:
                input_file_uris["weather_file_uri"] = weather_uri

        return input_file_uris

    def _read_output_envelope(
        self,
        output_path: str,
    ) -> ValidationOutputEnvelope | None:
        """
        Read and parse the output envelope from storage.

        Returns None if the file doesn't exist or can't be parsed.
        """
        try:
            output_data = self.storage.read(output_path)
            if output_data is None:
                logger.error("Output envelope not found at %s", output_path)
                return None

            # Parse as JSON first to get the validator type
            if isinstance(output_data, bytes):
                output_data = output_data.decode("utf-8")
            output_dict = json.loads(output_data)

            # Determine envelope class based on validator type
            validator_type = output_dict.get("validator", {}).get("type", "").upper()

            if validator_type == "ENERGYPLUS":
                from validibot_shared.energyplus.envelopes import EnergyPlusOutputEnvelope

                return EnergyPlusOutputEnvelope.model_validate(output_dict)
            if validator_type == "FMI":
                from validibot_shared.fmi.envelopes import FMIOutputEnvelope

                return FMIOutputEnvelope.model_validate(output_dict)
            # Generic envelope
            from validibot_shared.validations.envelopes import ValidationOutputEnvelope

            return ValidationOutputEnvelope.model_validate(output_dict)

        except Exception:
            logger.exception("Failed to read output envelope from %s", output_path)
            return None

    def _get_callback_url(self) -> str:
        """
        Get the callback URL (unused for sync execution, but needed for envelope).

        Returns a placeholder URL since sync execution doesn't use callbacks.
        """
        # Use SITE_URL if available, otherwise a placeholder
        site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
        return f"{site_url.rstrip('/')}/api/v1/validation-callbacks/"
