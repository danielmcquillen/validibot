"""Tests for Cloud Run Service provider-task dispatch and adapter selection.

The Service path must preserve the execution-attempt lifecycle already used by
Jobs while changing the provider handoff.  These tests pin deterministic task
identity, child-request authority, acceptance ambiguity, duplicate delivery,
and explicit unsupported provider-status lookup.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone

from validibot.core.tasks.dispatch.http_task_client import HttpTaskCreationResult
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ProviderStatusLookupCapability
from validibot.validations.services.cloud_run.launcher import (
    ProviderDispatchAmbiguousError,
)
from validibot.validations.services.execution.gcp_service import (
    CloudRunServiceExecutionBackend,
)
from validibot.validations.services.execution.gcp_service_dispatch import (
    dispatch_cloud_run_service_validation,
)

PROJECT_ID = "validibot-prod"
REGION = "australia-southeast1"
QUEUE_NAME = "validator-provider-prod"
SERVICE_NAME = "validibot-energyplus"
SERVICE_REVISION = "validibot-energyplus-00001-abc"
DIGEST = "sha256:" + "b" * 64
INVOKER = "validator-invoker@validibot-prod.iam.gserviceaccount.com"
TEST_CAPABILITY_VALUE = "opaque-test-capability"


def _attempt(*, state=ExecutionAttemptState.PENDING):
    """Return one pinned attempt/deployment pair for dispatcher unit tests."""
    attempt_id = uuid4()
    deployment_id = uuid4()
    deployment = SimpleNamespace(
        pk=deployment_id,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        deployment_revision=SERVICE_REVISION,
        provider_configuration={
            "project_id": PROJECT_ID,
            "region": REGION,
            "service_name": SERVICE_NAME,
        },
        provider_resource_name=(
            f"projects/{PROJECT_ID}/locations/{REGION}/services/{SERVICE_NAME}"
        ),
        backend_image_digest=DIGEST,
        route="https://validator.example",
        authentication_audience="https://validator.example",
        maximum_execution_seconds=1500,
    )
    attempt = SimpleNamespace(
        pk=attempt_id,
        deployment_id=deployment_id,
        deployment=deployment,
        timeout_at=timezone.now() + timedelta(minutes=20),
        state=state,
        provider_execution_id=(
            "existing-task" if state == ExecutionAttemptState.RUNNING else ""
        ),
    )
    return attempt, deployment


def _capability():
    """Return transient child authority without involving Google credentials."""
    return SimpleNamespace(
        access_token=TEST_CAPABILITY_VALUE,
        expires_at=timezone.now() + timedelta(minutes=30),
        allowed_prefix="gs://bucket/runs/attempt/",
        project_id=PROJECT_ID,
        refresh_url="https://worker.example/refresh/",
    )


@override_settings(
    GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED=True,
    GCP_VALIDATOR_TASK_QUEUE_NAME=QUEUE_NAME,
    GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT=INVOKER,
    GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS=1800,
)
def test_dispatch_claims_attempt_and_creates_one_deterministic_provider_task(
    monkeypatch,
):
    """Provider acceptance must bind the exact task, deployment, and image."""
    attempt, deployment = _attempt()
    task_name = (
        f"projects/{PROJECT_ID}/locations/{REGION}/queues/{QUEUE_NAME}/tasks/"
        f"{attempt.pk}"
    )
    transitions = []
    create_task = MagicMock(
        return_value=HttpTaskCreationResult(task_name=task_name, created=True)
    )
    mark_running = MagicMock()
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "get_active_execution_attempt",
        lambda _step_run: attempt,
    )

    def _transition(_attempt_id, target, **kwargs):
        transitions.append((target, kwargs))
        attempt.state = target
        if target == ExecutionAttemptState.RUNNING:
            attempt.provider_execution_id = kwargs["provider_execution_id"]
        return attempt, True

    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "transition_execution_attempt",
        _transition,
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "issue_attempt_gcs_runtime_capability",
        lambda **_kwargs: _capability(),
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "create_http_task",
        create_task,
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "_mark_step_run_running",
        mark_running,
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "build_input_evidence_snapshot",
        lambda *_args, **_kwargs: {"attempt_contract_version": "v2"},
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "sha256_hex_for_model",
        lambda _envelope: "c" * 64,
    )
    envelope = SimpleNamespace(
        context=SimpleNamespace(
            expected_output_uri="gs://bucket/runs/attempt/output.json"
        )
    )

    execution_id, image_digest = dispatch_cloud_run_service_validation(
        step_run=SimpleNamespace(pk="step-1"),
        job_name=SERVICE_NAME,
        input_envelope_uri="gs://bucket/runs/attempt/input.json",
        execution_bundle_uri="gs://bucket/runs/attempt",
        envelope=envelope,
        submission=object(),
        step=object(),
        expected_image_digest=DIGEST,
    )

    assert execution_id == task_name
    assert image_digest == DIGEST
    assert [target for target, _kwargs in transitions] == [
        ExecutionAttemptState.DISPATCHING,
        ExecutionAttemptState.RUNNING,
    ]
    call = create_task.call_args.kwargs
    assert call["task_id"] == str(attempt.pk)
    assert call["payload"]["deployment_id"] == str(deployment.pk)
    assert call["payload"]["gcs_capability"]["access_token"] == (TEST_CAPABILITY_VALUE)
    mark_running.assert_called_once()


@override_settings(
    GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED=True,
    GCP_VALIDATOR_TASK_QUEUE_NAME=QUEUE_NAME,
    GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT=INVOKER,
    GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS=1800,
)
def test_ambiguous_provider_task_create_moves_attempt_to_unknown(monkeypatch):
    """A create exception cannot authorize a second task name or Job fallback."""
    attempt, _deployment = _attempt()
    transitions = []
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "get_active_execution_attempt",
        lambda _step_run: attempt,
    )

    def _transition(_attempt_id, target, **_kwargs):
        transitions.append(target)
        attempt.state = target
        return attempt, True

    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "transition_execution_attempt",
        _transition,
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "issue_attempt_gcs_runtime_capability",
        lambda **_kwargs: _capability(),
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "create_http_task",
        MagicMock(side_effect=RuntimeError("ambiguous")),
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "build_input_evidence_snapshot",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "sha256_hex_for_model",
        lambda _envelope: "c" * 64,
    )
    envelope = SimpleNamespace(
        context=SimpleNamespace(
            expected_output_uri="gs://bucket/runs/attempt/output.json"
        )
    )

    with pytest.raises(ProviderDispatchAmbiguousError):
        dispatch_cloud_run_service_validation(
            step_run=SimpleNamespace(pk="step-1"),
            job_name=SERVICE_NAME,
            input_envelope_uri="gs://bucket/runs/attempt/input.json",
            execution_bundle_uri="gs://bucket/runs/attempt",
            envelope=envelope,
            submission=object(),
            step=object(),
            expected_image_digest=DIGEST,
        )

    assert transitions == [
        ExecutionAttemptState.DISPATCHING,
        ExecutionAttemptState.UNKNOWN,
    ]


@override_settings(
    GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED=True,
    GCP_VALIDATOR_TASK_QUEUE_NAME=QUEUE_NAME,
    GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT=INVOKER,
    GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS=1800,
)
def test_unknown_redelivery_recovers_only_the_same_deterministic_task(monkeypatch):
    """Ambiguous acceptance may rebind its task ID but never mint another task."""
    attempt, _deployment = _attempt(state=ExecutionAttemptState.UNKNOWN)
    task_name = (
        f"projects/{PROJECT_ID}/locations/{REGION}/queues/{QUEUE_NAME}/tasks/"
        f"{attempt.pk}"
    )
    transitions = []
    create_task = MagicMock(
        return_value=HttpTaskCreationResult(task_name=task_name, created=False)
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "get_active_execution_attempt",
        lambda _step_run: attempt,
    )

    def _transition(_attempt_id, target, **kwargs):
        transitions.append(target)
        attempt.state = target
        attempt.provider_execution_id = kwargs.get("provider_execution_id", "")
        return attempt, True

    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "transition_execution_attempt",
        _transition,
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "issue_attempt_gcs_runtime_capability",
        lambda **_kwargs: _capability(),
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "create_http_task",
        create_task,
    )
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service_dispatch."
        "_mark_step_run_running",
        MagicMock(),
    )
    envelope = SimpleNamespace(
        context=SimpleNamespace(
            expected_output_uri="gs://bucket/runs/attempt/output.json"
        )
    )

    execution_id, _digest = dispatch_cloud_run_service_validation(
        step_run=SimpleNamespace(pk="step-1"),
        job_name=SERVICE_NAME,
        input_envelope_uri="gs://bucket/runs/attempt/input.json",
        execution_bundle_uri="gs://bucket/runs/attempt",
        envelope=envelope,
        submission=object(),
        step=object(),
        expected_image_digest=DIGEST,
    )

    assert execution_id == task_name
    assert transitions == [ExecutionAttemptState.RUNNING]
    assert create_task.call_args.kwargs["task_id"] == str(attempt.pk)


def test_service_backend_declares_status_lookup_unsupported():
    """Reconciliation cannot mistake absence of a request resource for failure."""
    _attempt_value, deployment = _attempt()
    backend = CloudRunServiceExecutionBackend(deployment=deployment)

    assert (
        backend.status_lookup_capability == ProviderStatusLookupCapability.UNSUPPORTED
    )
    assert backend.check_status("provider-task") is None


def test_service_backend_cancels_the_exact_provider_task(monkeypatch):
    """Cancellation may delete only the task identity pinned to the attempt."""
    _attempt_value, deployment = _attempt()
    client = MagicMock()
    monkeypatch.setattr(
        "validibot.validations.services.execution.gcp_service."
        "tasks_v2.CloudTasksClient",
        MagicMock(return_value=client),
    )
    backend = CloudRunServiceExecutionBackend(deployment=deployment)
    task_name = (
        f"projects/{PROJECT_ID}/locations/{REGION}/queues/{QUEUE_NAME}/tasks/attempt-id"
    )

    canceled = backend.cancel(task_name)

    assert canceled is True
    client.delete_task.assert_called_once_with(name=task_name)
