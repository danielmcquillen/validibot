"""End-to-end tests for the minimal execution-attempt lifecycle.

The lifecycle writer exists to close two costly failure windows without
introducing a generic durable-work framework: duplicate delivery must reuse one
attempt, and a provider call that raises after possible acceptance must become
UNKNOWN rather than launch again. These tests exercise the concrete writer and
Cloud Run dispatch boundary where those guarantees are established.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from validibot_shared.validations.envelopes import ValidationCallback
from validibot_shared.validations.envelopes import ValidationStatus

from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.cloud_run.launcher import (
    ProviderDispatchAmbiguousError,
)
from validibot.validations.services.cloud_run.launcher import _attempt_execution_bundle
from validibot.validations.services.cloud_run.launcher import _run_validator_job_safely
from validibot.validations.services.create_only_storage import StorageConflictError
from validibot.validations.services.execution_attempts import build_attempt_callback_id
from validibot.validations.services.execution_attempts import (
    get_or_create_execution_attempt,
)
from validibot.validations.services.execution_attempts import resolve_callback_attempt
from validibot.validations.services.execution_attempts import (
    transition_execution_attempt,
)
from validibot.validations.services.step_orchestrator import StepOrchestrator
from validibot.validations.services.validation_callback import ValidationCallbackService
from validibot.validations.services.validation_run import fence_active_execution_attempt
from validibot.validations.tests.factories import CallbackReceiptFactory
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory


@pytest.mark.django_db
class TestExecutionAttemptWriter:
    """Prove attempt allocation and callback identity are deterministic."""

    def test_duplicate_preparation_reuses_the_active_attempt(self):
        """Two task deliveries must converge before either can launch compute."""
        step_run = ValidationStepRunFactory()

        first, first_created = get_or_create_execution_attempt(
            step_run,
            runner_type="GCPExecutionBackend",
        )
        second, second_created = get_or_create_execution_attempt(
            step_run,
            runner_type="GCPExecutionBackend",
        )

        assert first_created is True
        assert second_created is False
        assert second == first
        assert step_run.execution_attempts.count() == 1

    def test_terminal_history_allocates_the_next_attempt_number(self):
        """An explicit later retry preserves history instead of mutating identity."""
        first = ExecutionAttemptFactory(state=ExecutionAttemptState.PENDING)
        transition_execution_attempt(first.pk, ExecutionAttemptState.FAILED)

        second, created = get_or_create_execution_attempt(
            first.step_run,
            runner_type=first.runner_type,
        )

        assert created is True
        assert second is not None
        assert second.attempt_number == first.attempt_number + 1

    def test_task_redelivery_does_not_retry_after_provider_completion(self):
        """A crash between provider completion and step finalization stays safe."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.COMPLETED)

        step_run, should_execute = StepOrchestrator()._start_step_run(
            validation_run=attempt.step_run.validation_run,
            workflow_step=attempt.step_run.workflow_step,
        )

        assert step_run == attempt.step_run
        assert should_execute is False
        assert step_run.execution_attempts.count() == 1

    def test_callback_id_resolves_only_inside_its_own_run(self):
        """An opaque callback key cannot be replayed against another tenant run."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.RUNNING)
        callback_id = build_attempt_callback_id(attempt)
        other_run = ValidationRunFactory()

        assert (
            resolve_callback_attempt(
                callback_id,
                run_id=attempt.step_run.validation_run_id,
            )
            == attempt
        )
        assert resolve_callback_attempt(callback_id, run_id=other_run.pk) is None
        assert (
            resolve_callback_attempt(
                "execution-attempt-not-a-uuid",
                run_id=other_run.pk,
            )
            is None
        )


@pytest.mark.django_db
class TestAttemptExecutionBundlePaths:
    """Prove every dispatch target is derived from the durable attempt UUID."""

    def test_gcs_bundle_is_nested_below_the_active_attempt(self, settings):
        """Cloud Run must not write envelopes into the mutable run root."""
        settings.GCS_VALIDATION_BUCKET = "validation-bucket"
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.PENDING)
        run = attempt.step_run.validation_run

        bundle = _attempt_execution_bundle(
            run=run,
            step_run=attempt.step_run,
        )

        expected = (
            f"gs://validation-bucket/runs/{run.org_id}/{run.pk}/attempts/{attempt.pk}"
        )
        assert bundle.execution_bundle_uri == expected
        assert bundle.input_envelope_uri == f"{expected}/input.json"
        assert bundle.local_dir is None

    def test_retry_receives_a_distinct_gcs_prefix(self, settings):
        """A new attempt must not reuse the failed attempt's object prefix."""
        settings.GCS_VALIDATION_BUCKET = "validation-bucket"
        first = ExecutionAttemptFactory(state=ExecutionAttemptState.PENDING)
        first_bundle = _attempt_execution_bundle(
            run=first.step_run.validation_run,
            step_run=first.step_run,
        )
        transition_execution_attempt(first.pk, ExecutionAttemptState.FAILED)
        retry, created = get_or_create_execution_attempt(
            first.step_run,
            runner_type=first.runner_type,
        )

        retry_bundle = _attempt_execution_bundle(
            run=retry.step_run.validation_run,
            step_run=retry.step_run,
        )

        assert created is True
        assert first_bundle.execution_bundle_uri != retry_bundle.execution_bundle_uri
        assert str(first.pk) in first_bundle.execution_bundle_uri
        assert str(retry.pk) in retry_bundle.execution_bundle_uri

    def test_local_async_bundle_uses_the_same_attempt_layout(
        self,
        settings,
        tmp_path,
    ):
        """Local asynchronous development must preserve production path shape."""
        settings.GCS_VALIDATION_BUCKET = ""
        settings.MEDIA_ROOT = tmp_path
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.PENDING)
        run = attempt.step_run.validation_run

        bundle = _attempt_execution_bundle(
            run=run,
            step_run=attempt.step_run,
        )

        expected = (
            tmp_path
            / "files"
            / "runs"
            / str(run.org_id)
            / str(run.pk)
            / "attempts"
            / str(attempt.pk)
        )
        assert bundle.local_dir == expected
        assert bundle.execution_bundle_uri == str(expected)
        assert bundle.input_envelope_uri == str(expected / "input.json")
        assert expected.is_dir()

    def test_local_async_bundle_rejects_duplicate_attempt_preparation(
        self,
        settings,
        tmp_path,
    ):
        """A redelivered local dispatch must not reuse an existing bundle."""
        settings.GCS_VALIDATION_BUCKET = ""
        settings.MEDIA_ROOT = tmp_path
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.PENDING)
        run = attempt.step_run.validation_run

        _attempt_execution_bundle(run=run, step_run=attempt.step_run)

        with pytest.raises(StorageConflictError, match="already exists"):
            _attempt_execution_bundle(run=run, step_run=attempt.step_run)


@pytest.mark.django_db
class TestCloudRunDispatchAmbiguity:
    """Fence the exact provider-acceptance window that can duplicate compute."""

    @patch("validibot.validations.services.cloud_run.launcher.run_validator_job")
    def test_success_records_provider_identity_before_redelivery(self, mock_run_job):
        """A normal provider response leaves one addressable RUNNING attempt."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.PENDING)
        mock_run_job.return_value = "projects/p/locations/r/executions/e-1"

        execution_id = _run_validator_job_safely(
            step_run=attempt.step_run,
            project_id="project",
            region="region",
            job_name="validator-job",
            input_uri="gs://bucket/input.json",
            execution_bundle_uri="gs://bucket/bundle",
            input_envelope_sha256="a" * 64,
            output_envelope_uri="gs://bucket/bundle/output.json",
        )

        attempt.refresh_from_db()
        assert execution_id == mock_run_job.return_value
        assert attempt.state == ExecutionAttemptState.RUNNING
        assert attempt.provider_execution_id == mock_run_job.return_value
        assert attempt.provider_job_name == "validator-job"
        assert attempt.execution_bundle_uri == "gs://bucket/bundle"
        assert attempt.input_envelope_sha256 == "a" * 64
        assert attempt.output_envelope_uri == "gs://bucket/bundle/output.json"

    @patch("validibot.validations.services.cloud_run.launcher.run_validator_job")
    def test_ambiguous_provider_error_is_never_relaunched(self, mock_run_job):
        """A timeout after possible acceptance must remain UNKNOWN on redelivery."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.PENDING)
        mock_run_job.side_effect = TimeoutError("response lost")
        kwargs = {
            "step_run": attempt.step_run,
            "project_id": "project",
            "region": "region",
            "job_name": "validator-job",
            "input_uri": "gs://bucket/input.json",
            "execution_bundle_uri": "gs://bucket/bundle",
            "input_envelope_sha256": "a" * 64,
            "output_envelope_uri": "gs://bucket/bundle/output.json",
        }

        with pytest.raises(ProviderDispatchAmbiguousError):
            _run_validator_job_safely(**kwargs)

        attempt.refresh_from_db()
        assert attempt.state == ExecutionAttemptState.UNKNOWN
        assert mock_run_job.call_count == 1

        mock_run_job.reset_mock()
        with pytest.raises(ProviderDispatchAmbiguousError):
            _run_validator_job_safely(**kwargs)
        mock_run_job.assert_not_called()


@pytest.mark.django_db
class TestAttemptCallbackCompletion:
    """Keep callback finalization bound to the concrete provider attempt."""

    def test_verified_callback_terminally_completes_its_attempt(self):
        """Successful output processing closes the same attempt named in delivery."""
        attempt = ExecutionAttemptFactory(
            state=ExecutionAttemptState.RUNNING,
            output_envelope_uri="gs://bucket/output.json",
        )
        run = attempt.step_run.validation_run
        receipt = CallbackReceiptFactory(
            callback_id=build_attempt_callback_id(attempt),
            validation_run=run,
            execution_attempt=attempt,
        )
        callback = ValidationCallback(
            run_id=str(run.pk),
            callback_id=receipt.callback_id,
            status=ValidationStatus.SUCCESS,
            result_uri="gs://bucket/output.json",
        )
        service = ValidationCallbackService()
        processing_result = MagicMock(step_status="PASSED")
        output_envelope = MagicMock()
        output_envelope.model_dump.return_value = {
            "schema_version": "validibot.output.v1",
            "run_id": str(run.pk),
        }

        with (
            patch.object(
                service,
                "_resolve_active_step_run",
                return_value=(attempt.step_run, MagicMock()),
            ),
            patch.object(
                service,
                "_download_and_validate_envelope",
                return_value=output_envelope,
            ),
            patch.object(
                service,
                "_complete_step",
                return_value=processing_result,
            ),
            patch.object(service, "_finalize_or_resume"),
            patch.object(service, "_mark_receipt_completed"),
        ):
            response = service._process_callback(
                callback=callback,
                run=run,
                receipt=receipt,
                attempt=attempt,
            )

        attempt.refresh_from_db()
        assert response.status_code == 200  # noqa: PLR2004
        assert attempt.state == ExecutionAttemptState.COMPLETED
        assert attempt.output_envelope_uri == "gs://bucket/output.json"
        assert len(attempt.output_envelope_sha256) == 64  # noqa: PLR2004

    def test_timeout_fences_attempt_before_provider_cancellation(self):
        """A late callback cannot win after the watchdog commits its decision."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.RUNNING)
        run = attempt.step_run.validation_run
        run.status = ValidationRunStatus.TIMED_OUT
        run.save(update_fields=["status"])

        fence_active_execution_attempt(
            run,
            target=ExecutionAttemptState.TIMED_OUT,
            error_code="run_timed_out",
            error_message="Outer execution deadline elapsed.",
        )

        attempt.refresh_from_db()
        assert attempt.state == ExecutionAttemptState.TIMED_OUT
