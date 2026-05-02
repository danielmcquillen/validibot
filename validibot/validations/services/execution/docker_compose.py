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

    def check_status(self, execution_id: str) -> ExecutionResponse | None:
        """
        Check the status of a Docker container execution.

        For Docker Compose, execution is synchronous so this is primarily
        useful for debugging. The runner's get_execution_status() queries
        the Docker daemon for the container's current state.

        Args:
            execution_id: Docker container ID (short or full).

        Returns:
            ExecutionResponse if the container exists, None if not found or
            if the Docker daemon is unavailable.
        """
        from validibot.validations.services.runners.base import ExecutionStatus

        try:
            info = self.runner.get_execution_status(execution_id)
        except ValueError:
            # Container not found — expected for expired/unknown container IDs
            return None
        except Exception:
            logger.warning(
                "Failed to check Docker container status for %s",
                execution_id,
                exc_info=True,
            )
            return None

        return ExecutionResponse(
            execution_id=info.execution_id,
            is_complete=info.status
            in (
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
            ),
            error_message=info.error_message,
        )

    def get_container_image(self, validator_type: str) -> str:
        """
        Get the container image for a validator type.

        Resolution order for the image base name:

        1. ``settings.VALIDATOR_IMAGES`` — deploy-time override mapping
           (still supported; use this for fully-qualified per-deploy images).
        2. ``ValidatorConfig.image_name`` — the validator's own declaration
           (the canonical source for system validators, set in each
           validator's ``config.py``).
        3. Convention fallback — ``validibot-validator-backend-{slug}``.

        Steps 2 and 3 then have ``VALIDATOR_IMAGE_TAG`` and
        ``VALIDATOR_IMAGE_REGISTRY`` applied. Step 1 is taken verbatim
        because callers configuring ``VALIDATOR_IMAGES`` typically supply
        a complete image reference already.
        """
        from validibot.validations.validators.base.config import get_config

        # Normalize validator type
        vtype = validator_type.lower()

        # 1. Deploy-time override mapping
        image_map = getattr(settings, "VALIDATOR_IMAGES", {})
        if vtype in image_map:
            return image_map[vtype]

        # 2. ValidatorConfig.image_name from the validator's own declaration
        config = get_config(vtype.upper())
        if config and config.image_name:
            image_name = config.image_name
        else:
            # 3. Convention fallback
            image_name = f"validibot-validator-backend-{vtype}"

        image_tag = getattr(settings, "VALIDATOR_IMAGE_TAG", "latest")
        registry = getattr(settings, "VALIDATOR_IMAGE_REGISTRY", "")

        if registry:
            return f"{registry}/{image_name}:{image_tag}"
        return f"{image_name}:{image_tag}"

    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute a validation synchronously via Docker.

        Dispatch builds a per-run workspace on the host, materialises
        only the files this run needs into ``input/``, rewrites
        envelope URIs to container paths, and runs the container
        against per-run mounts. No validator backend changes are
        required — backends are URI-driven and resolve the
        container-visible URIs directly.

        Steps:

        1. Build the per-run workspace (input/, output/, materialised
           primary file + resource files).
        2. Build the input envelope with container-visible URIs.
        3. Write the envelope into the workspace's input directory.
        4. Run the validator container with per-run mounts.
        5. Read the output envelope from the workspace's output
           directory.

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

        execution_id = str(uuid.uuid4())[:12]

        try:
            # 1. Build the per-run workspace and the envelope override
            # kwargs that point envelope URIs at container paths.
            workspace, input_file_uris, resource_uri_overrides = (
                self._build_workspace_and_envelope_kwargs(request)
            )

            # 2. Build callback URL and ID (unused for sync, but the
            # envelope schema still requires the fields).
            callback_url = self._get_callback_url()
            callback_id = f"step-run-{request.run.current_step_run.id}"

            # 3. Build the envelope with container-visible URIs.
            envelope = self.build_input_envelope(
                request,
                callback_url=callback_url,
                callback_id=callback_id,
                execution_bundle_uri=workspace.execution_bundle_container_uri,
                input_file_uris=input_file_uris,
                resource_uri_overrides=resource_uri_overrides,
            )

            # 4. Write the envelope into the workspace's input
            # directory. The container will read it through the
            # read-only ``/validibot/input`` mount.
            envelope_json = envelope.model_dump_json(indent=2)
            workspace.host_input_envelope_path.write_bytes(
                envelope_json.encode("utf-8"),
            )
            workspace.host_input_envelope_path.chmod(0o644)

            logger.info(
                "Wrote input envelope for run %s to %s",
                request.run_id,
                workspace.host_input_envelope_path,
            )

            # 5. Resolve container image.
            container_image = self.get_container_image(request.validator_type)

            logger.info(
                "Executing container: image=%s, input=%s, output=%s",
                container_image,
                workspace.input_envelope_container_uri,
                workspace.output_envelope_container_uri,
            )

            # 6. Run the container with per-run mounts.
            result = self.runner.run(
                container_image=container_image,
                input_uri=workspace.input_envelope_container_uri,
                output_uri=workspace.output_envelope_container_uri,
                run_id=str(request.run_id),
                validator_slug=request.validator_type.lower(),
                workspace=workspace,
            )

            # 7. Process the result.
            if not result.succeeded:
                # Include truncated container logs in the error message so the
                # user can see *why* the container failed, not just the exit code.
                error_parts = [
                    result.error_message
                    or f"Container exited with code {result.exit_code}",
                ]
                if result.logs:
                    # Truncate to last 2000 chars to avoid huge findings.
                    log_tail = result.logs[-2000:].strip()
                    if log_tail:
                        error_parts.append(f"Container output:\n{log_tail}")

                error_msg = "\n\n".join(error_parts)

                logger.warning(
                    "Container failed for run %s: exit_code=%d, error=%s",
                    request.run_id,
                    result.exit_code,
                    error_msg,
                )
                return ExecutionResponse(
                    execution_id=result.execution_id or execution_id,
                    is_complete=True,
                    error_message=error_msg,
                    input_uri=workspace.input_envelope_container_uri,
                    output_uri=workspace.output_envelope_container_uri,
                    execution_bundle_uri=workspace.execution_bundle_container_uri,
                    duration_seconds=result.duration_seconds,
                    validator_backend_image_digest=(
                        result.validator_backend_image_digest
                    ),
                )

            # 8. Read the output envelope from the workspace.
            output_envelope = self._read_output_envelope_from_host(
                workspace.host_output_envelope_path,
            )

            logger.info(
                "Container execution completed for run %s in %.2fs",
                request.run_id,
                result.duration_seconds,
            )

            return ExecutionResponse(
                execution_id=result.execution_id or execution_id,
                is_complete=True,
                output_envelope=output_envelope,
                input_uri=workspace.input_envelope_container_uri,
                output_uri=workspace.output_envelope_container_uri,
                execution_bundle_uri=workspace.execution_bundle_container_uri,
                duration_seconds=result.duration_seconds,
                validator_backend_image_digest=(result.validator_backend_image_digest),
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

    # ── Workspace dispatch helpers ──────────────────────────────────────

    def _build_workspace_and_envelope_kwargs(
        self,
        request: ExecutionRequest,
    ) -> tuple[object, dict[str, str], dict[str, str]]:
        """Build the per-run workspace and the envelope override kwargs.

        Materialises the primary submission file plus every workflow
        step resource the envelope will reference (weather files for
        EnergyPlus, FMU model files for FMU). Returns the workspace
        plus two dicts that feed straight into ``build_input_envelope``:

        - ``input_file_uris``: the primary file URI (and the FMU model
          URI when applicable) keyed by the role names the envelope
          builder reads.
        - ``resource_uri_overrides``: ``resource_id`` →
          container-visible URI, used by the EnergyPlus envelope to
          point ``ResourceFileItem.uri`` at the per-run mount instead
          of the host ``MEDIA_ROOT`` path.

        The translation is local-Docker-specific: it expects every
        resource's ``get_storage_uri()`` to return ``file://`` URIs
        (the GCS path is unreachable from inside a local container).
        Non-file URIs raise an explicit error rather than silently
        skipping.
        """

        from pathlib import Path

        from validibot.validations.constants import ValidationType
        from validibot.validations.services.run_workspace import ResourceFileSpec
        from validibot.validations.services.run_workspace import RunWorkspaceBuilder
        from validibot.workflows.models import WorkflowStepResource

        step = request.run.current_step_run.workflow_step
        validator_type_upper = request.validator_type.upper()

        # Collect resource specs to materialise. Skip MODEL_TEMPLATE —
        # it's consumed during EnergyPlus preprocessing and would
        # collide with the resolved primary model file.
        resource_specs: list[ResourceFileSpec] = []
        fmu_resource_id: str | None = None

        for sr in step.step_resources.select_related("validator_resource_file").all():
            if sr.role == WorkflowStepResource.MODEL_TEMPLATE:
                continue

            if sr.is_catalog_reference:
                vrf = sr.validator_resource_file
                resource_id = str(vrf.id)
                filename = vrf.filename
                uri = vrf.get_storage_uri()
            else:
                resource_id = str(sr.pk)
                filename = sr.filename or Path(sr.step_resource_file.name).name
                uri = sr.get_storage_uri()

            if not uri.startswith("file://"):
                msg = (
                    f"DockerComposeExecutionBackend cannot materialise "
                    f"non-file URI: {uri} (resource id {resource_id}). "
                    f"Local Docker dispatch requires file:// URIs."
                )
                raise RuntimeError(msg)

            source_path = Path(uri[len("file://") :])

            resource_specs.append(
                ResourceFileSpec(
                    filename=filename,
                    source_path=source_path,
                    resource_id=resource_id,
                ),
            )

            # Track the FMU model resource so we can override the
            # envelope's ``input_files[0].uri`` (FMU envelopes use the
            # special ``input_file_uris["fmu_model_uri"]`` key, not the
            # generic resource_uri_overrides path).
            if (
                validator_type_upper == ValidationType.FMU
                and sr.role == WorkflowStepResource.FMU_MODEL
            ):
                fmu_resource_id = resource_id

        # Build the workspace.
        builder = RunWorkspaceBuilder(storage=self.storage)
        primary_content = request.submission.get_content()
        if isinstance(primary_content, str):
            primary_bytes = primary_content.encode("utf-8")
        else:
            primary_bytes = primary_content
        primary_filename = request.submission.original_filename or "submission"

        workspace = builder.build(
            org_id=str(request.org_id),
            run_id=str(request.run_id),
            primary_filename=primary_filename,
            primary_content=primary_bytes,
            resource_files=resource_specs,
        )

        # Build override kwargs the envelope builder consumes.
        resource_uri_overrides = {
            mf.resource_id: mf.container_uri
            for mf in workspace.resource_files
            if mf.resource_id is not None
        }

        input_file_uris: dict[str, str] = {
            "primary_file_uri": workspace.primary_file.container_uri,
        }

        if fmu_resource_id is not None:
            fmu_container_uri = next(
                (
                    mf.container_uri
                    for mf in workspace.resource_files
                    if mf.resource_id == fmu_resource_id
                ),
                None,
            )
            if fmu_container_uri is not None:
                input_file_uris["fmu_model_uri"] = fmu_container_uri

        return workspace, input_file_uris, resource_uri_overrides

    def _read_output_envelope_from_host(
        self,
        host_path,
    ) -> ValidationOutputEnvelope | None:
        """Read and parse the output envelope from a host path.

        The container writes ``output.json`` into ``/validibot/output``
        which the host sees at ``workspace.host_output_envelope_path``.
        We read it directly from disk rather than through the storage
        abstraction because the workspace already knows its host path
        — going through the storage layer would require translating
        the host path back to a storage-relative path for no benefit.

        Returns ``None`` if the file is missing (which the
        run-completion contract treats as "container died
        unexpectedly") or the envelope is unparseable.
        """

        try:
            if not host_path.exists():
                logger.error("Output envelope not found at %s", host_path)
                return None

            output_data = host_path.read_bytes()

            output_dict = json.loads(output_data.decode("utf-8"))

            # Look up the output envelope class from the registry using the
            # validator type embedded in the envelope JSON.
            from validibot.validations.validators.base.config import (
                get_output_envelope_class,
            )

            validator_type = output_dict.get("validator", {}).get("type", "").upper()
            envelope_class = get_output_envelope_class(validator_type)

            if envelope_class is None:
                # Fall back to the generic base envelope for unknown types.
                from validibot_shared.validations.envelopes import (
                    ValidationOutputEnvelope,
                )

                envelope_class = ValidationOutputEnvelope

            return envelope_class.model_validate(output_dict)

        except Exception:
            logger.exception("Failed to read output envelope from %s", host_path)
            return None

    def _get_callback_url(self) -> str:
        """
        Get the callback URL (unused for sync execution, but needed for envelope).

        Returns a placeholder URL since sync execution doesn't use callbacks.
        """
        # Use SITE_URL if available, otherwise a placeholder
        site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
        return f"{site_url.rstrip('/')}/api/v1/validation-callbacks/"
