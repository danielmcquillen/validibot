"""Tests for Cloud Run execution overrides at the storage trust boundary.

The job trigger is the only transport for a per-attempt downscoped token. This
suite verifies all capability fields reach the container while the bearer token
is absent from application logs. The token is never placed in durable launch
stats or the execution-attempt database.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
    AttemptGCSRuntimeCapability,
)
from validibot.validations.services.cloud_run.job_client import (
    clear_cloud_run_client_caches,
)
from validibot.validations.services.cloud_run.job_client import run_validator_job

EXPECTED_JOB_DISPATCH_COUNT = 2


@pytest.fixture(autouse=True)
def _clear_client_caches():
    """Keep patched shared clients isolated across Job transport tests."""
    clear_cloud_run_client_caches()
    yield
    clear_cloud_run_client_caches()


@patch("validibot.validations.services.cloud_run.job_client.run_v2.JobsClient")
def test_run_job_delivers_capability_without_logging_token(jobs_client, caplog):
    """Cloud Run receives the bounded credential but ordinary logs never do."""
    operation = MagicMock()
    operation.metadata.name = "projects/p/locations/r/executions/e-1"
    jobs_client.return_value.run_job.return_value = operation
    capability = AttemptGCSRuntimeCapability(
        access_token="short-lived-secret-token",  # noqa: S106 - test fixture
        expires_at=datetime.now(UTC) + timedelta(minutes=50),
        allowed_prefix="gs://validation/runs/org/run/attempts/attempt/",
        project_id="validibot-project",
        refresh_url=(
            "https://worker.example/api/v1/validation-storage-capabilities/refresh/"
        ),
    )

    with caplog.at_level(logging.INFO):
        execution_name = run_validator_job(
            project_id="validibot-project",
            region="australia-southeast1",
            job_name="validibot-validator-backend-energyplus",
            input_uri="gs://validation/runs/org/run/attempts/attempt/input.json",
            gcs_capability=capability,
        )

    request = jobs_client.return_value.run_job.call_args.kwargs["request"]
    env = {
        item.name: item.value for item in request.overrides.container_overrides[0].env
    }
    assert execution_name == operation.metadata.name
    assert env["VALIDIBOT_GCS_CAPABILITY_REQUIRED"] == "1"
    assert env["VALIDIBOT_GCS_ACCESS_TOKEN"] == "short-lived-secret-token"  # noqa: S105
    assert env["VALIDIBOT_GCS_ALLOWED_PREFIX"] == capability.allowed_prefix
    assert "short-lived-secret-token" not in caplog.text


@patch("validibot.validations.services.cloud_run.job_client.run_v2.JobsClient")
def test_run_job_reuses_one_process_client(jobs_client):
    """Two Job launches should reuse one gRPC channel and credential cache."""
    operation = MagicMock()
    operation.metadata.name = "projects/p/locations/r/executions/e-1"
    jobs_client.return_value.run_job.return_value = operation
    kwargs = {
        "project_id": "validibot-project",
        "region": "australia-southeast1",
        "job_name": "validibot-validator-backend-energyplus",
        "input_uri": "gs://validation/attempt/input.json",
    }

    run_validator_job(**kwargs)
    run_validator_job(**kwargs)

    jobs_client.assert_called_once_with()
    assert jobs_client.return_value.run_job.call_count == EXPECTED_JOB_DISPATCH_COUNT
