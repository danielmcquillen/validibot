"""GCP execution backend using private Cloud Run Services and Cloud Tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.api_core.exceptions import NotFound
from google.cloud import tasks_v2

from validibot.validations.constants import ProviderStatusLookupCapability
from validibot.validations.services.execution.gcp import CloudRunJobsExecutionBackend
from validibot.validations.services.execution.gcp_service_dispatch import (
    dispatch_cloud_run_service_validation,
)

if TYPE_CHECKING:
    from validibot.validations.models import ValidatorExecutionDeployment
    from validibot.validations.services.execution.base import ExecutionResponse


class CloudRunServiceExecutionBackend(CloudRunJobsExecutionBackend):
    """Request-driven adapter selected only by a pinned Service deployment."""

    def __init__(self, *, deployment: ValidatorExecutionDeployment) -> None:
        """Require an exact deployment; there is no setting-only Service route."""
        super().__init__(deployment=deployment)

    @property
    def status_lookup_capability(self) -> ProviderStatusLookupCapability:
        """Cloud Run requests do not expose durable per-request status."""
        return ProviderStatusLookupCapability.UNSUPPORTED

    def check_status(self, execution_id: str) -> ExecutionResponse | None:
        """Return no provider status; output salvage and deadlines reconcile."""
        return None

    def cancel(self, execution_id: str) -> bool:
        """Delete the exact provider task when dispatch has not yet begun.

        Cloud Tasks cannot interrupt an HTTP request after delivery. A missing
        task therefore means best-effort cancellation is no longer available;
        the already-committed logical fence still makes any callback late.
        """
        try:
            tasks_v2.CloudTasksClient().delete_task(name=execution_id)
        except NotFound:
            return False
        return True

    @property
    def service_deployment(self) -> ValidatorExecutionDeployment:
        """Return the deployment required by this backend's constructor."""
        if self.deployment is None:  # Defensive against future base-class changes.
            raise RuntimeError("Cloud Run Service backend has no deployment.")
        return self.deployment

    def get_container_image(self, validator_type: str) -> str:
        """Return the exact private Service target recorded by deployment."""
        return str(self.service_deployment.provider_configuration["service_name"])

    def _launcher_kwargs(self, validator_type: str) -> dict:
        """Supply Service dispatch while reusing provider-neutral staging."""
        return {
            "job_name": self.get_container_image(validator_type),
            "expected_image_digest": self.service_deployment.backend_image_digest,
            "provider_dispatch": dispatch_cloud_run_service_validation,
        }
