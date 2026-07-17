"""Tests for the execution-attempt aggregate and state graph.

An attempt is the durable identity for one concrete provider launch.  These
tests focus on its core invariants: one active attempt per logical step, unique
provider identities, monotonic terminal state, and provider identity lookup
that never falls back to mutable step output.
"""

from itertools import product

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import transaction

from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.models import CallbackReceipt
from validibot.validations.models import ExecutionAttempt
from validibot.validations.services.execution_attempts import (
    InvalidExecutionAttemptTransitionError,
)
from validibot.validations.services.execution_attempts import (
    get_active_execution_attempt,
)
from validibot.validations.services.execution_attempts import (
    is_attempt_transition_allowed,
)
from validibot.validations.services.execution_attempts import (
    resolve_provider_execution_identity,
)
from validibot.validations.services.execution_attempts import (
    transition_execution_attempt,
)
from validibot.validations.tests.factories import CallbackReceiptFactory
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationStepRunFactory

EXPECTED_ATTEMPT_COUNT = 2
MAX_ERROR_CODE_LENGTH = 64
MAX_ERROR_LENGTH = 2000


@pytest.mark.django_db
class TestExecutionAttemptModel:
    """Enforce relational identity and active-attempt invariants in PostgreSQL."""

    def test_one_step_cannot_have_two_active_attempts(self):
        """Competing dispatchers must not create two billable provider launches."""
        first = ExecutionAttemptFactory(state=ExecutionAttemptState.RUNNING)

        with pytest.raises(IntegrityError), transaction.atomic():
            ExecutionAttemptFactory(
                step_run=first.step_run,
                attempt_number=2,
                state=ExecutionAttemptState.PENDING,
            )

    def test_terminal_history_allows_a_later_active_attempt(self):
        """A retry needs a new identity while preserving the completed history."""
        first = ExecutionAttemptFactory(state=ExecutionAttemptState.FAILED)

        second = ExecutionAttemptFactory(
            step_run=first.step_run,
            attempt_number=2,
            state=ExecutionAttemptState.PENDING,
        )

        assert second.pk is not None
        assert first.step_run.execution_attempts.count() == EXPECTED_ATTEMPT_COUNT

    def test_attempt_number_is_unique_within_a_step(self):
        """A stable sequence number lets operators and retry policy order attempts."""
        first = ExecutionAttemptFactory(state=ExecutionAttemptState.FAILED)

        with pytest.raises(IntegrityError), transaction.atomic():
            ExecutionAttemptFactory(
                step_run=first.step_run,
                attempt_number=first.attempt_number,
                state=ExecutionAttemptState.COMPLETED,
            )

    def test_provider_identity_is_unique_within_runner_and_job(self):
        """Two attempt rows must not claim the same concrete provider execution."""
        first = ExecutionAttemptFactory(
            state=ExecutionAttemptState.RUNNING,
            runner_type="google_cloud_run",
            provider_job_name="energyplus",
            provider_execution_id="executions/provider-123",
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            ExecutionAttemptFactory(
                runner_type=first.runner_type,
                provider_job_name=first.provider_job_name,
                provider_execution_id=first.provider_execution_id,
            )

    def test_callback_receipt_requires_attempt_identity(self):
        """Every accepted callback must remain attributable to its provider work."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.RUNNING)
        attempt_receipt = CallbackReceiptFactory(
            validation_run=attempt.step_run.validation_run,
            execution_attempt=attempt,
        )

        assert attempt_receipt.execution_attempt_id == attempt.id

    def test_callback_receipt_attempt_must_belong_to_same_run(self):
        """A callback identity must not be attachable to another customer's run."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.RUNNING)
        receipt = CallbackReceiptFactory(execution_attempt=attempt)

        with pytest.raises(ValidationError, match="must belong to the same run"):
            receipt.full_clean()

    def test_run_retention_can_delete_attempt_and_receipt_together(self):
        """Restricting standalone attempt deletion must not block run retention."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.RUNNING)
        receipt = CallbackReceiptFactory(
            validation_run=attempt.step_run.validation_run,
            execution_attempt=attempt,
        )
        attempt_id = attempt.id
        receipt_id = receipt.id

        attempt.step_run.validation_run.delete()

        assert not ExecutionAttempt.objects.filter(pk=attempt_id).exists()
        assert not CallbackReceipt.objects.filter(pk=receipt_id).exists()


@pytest.mark.django_db
class TestExecutionAttemptTransitions:
    """Keep all lifecycle writers on one small monotonic transition graph."""

    def test_every_state_pair_matches_the_accepted_graph(self):
        """Exhaustive edges catch accidental reopen or retry behavior changes."""
        allowed_targets = {
            ExecutionAttemptState.PENDING: {
                ExecutionAttemptState.PENDING,
                ExecutionAttemptState.DISPATCHING,
                ExecutionAttemptState.FAILED,
                ExecutionAttemptState.CANCELED,
            },
            ExecutionAttemptState.DISPATCHING: {
                ExecutionAttemptState.DISPATCHING,
                ExecutionAttemptState.RUNNING,
                ExecutionAttemptState.UNKNOWN,
                ExecutionAttemptState.FAILED,
                ExecutionAttemptState.CANCELED,
                ExecutionAttemptState.TIMED_OUT,
            },
            ExecutionAttemptState.RUNNING: {
                ExecutionAttemptState.RUNNING,
                ExecutionAttemptState.COMPLETED,
                ExecutionAttemptState.FAILED,
                ExecutionAttemptState.CANCELED,
                ExecutionAttemptState.TIMED_OUT,
            },
            ExecutionAttemptState.UNKNOWN: {
                ExecutionAttemptState.UNKNOWN,
                ExecutionAttemptState.RUNNING,
                ExecutionAttemptState.COMPLETED,
                ExecutionAttemptState.FAILED,
                ExecutionAttemptState.CANCELED,
                ExecutionAttemptState.TIMED_OUT,
            },
            ExecutionAttemptState.COMPLETED: {ExecutionAttemptState.COMPLETED},
            ExecutionAttemptState.FAILED: {ExecutionAttemptState.FAILED},
            ExecutionAttemptState.CANCELED: {ExecutionAttemptState.CANCELED},
            ExecutionAttemptState.TIMED_OUT: {ExecutionAttemptState.TIMED_OUT},
        }

        for current, target in product(ExecutionAttemptState, repeat=2):
            attempt = ExecutionAttemptFactory(state=current)
            expected = target in allowed_targets[current]
            assert is_attempt_transition_allowed(current, target) is expected

            if expected:
                transitioned, changed = transition_execution_attempt(
                    attempt.id,
                    target,
                )
                assert transitioned.state == target
                assert changed is (target != current)
            else:
                with pytest.raises(InvalidExecutionAttemptTransitionError):
                    transition_execution_attempt(attempt.id, target)

    def test_dispatch_and_terminal_timestamps_are_set_by_transition(self):
        """Lifecycle timestamps must come from the authoritative state writer."""
        attempt = ExecutionAttemptFactory()

        dispatching, _ = transition_execution_attempt(
            attempt.id,
            ExecutionAttemptState.DISPATCHING,
        )
        running, _ = transition_execution_attempt(
            attempt.id,
            ExecutionAttemptState.RUNNING,
        )
        completed, _ = transition_execution_attempt(
            attempt.id,
            ExecutionAttemptState.COMPLETED,
            provider_status_code="SUCCEEDED",
        )

        assert dispatching.dispatch_started_at is not None
        assert running.terminal_at is None
        assert completed.terminal_at is not None
        assert completed.provider_status_code == "SUCCEEDED"

    def test_diagnostics_are_sanitized_and_bounded(self):
        """Provider text must not inject controls or grow operational rows forever."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.RUNNING)

        failed, _ = transition_execution_attempt(
            attempt.id,
            ExecutionAttemptState.FAILED,
            last_error_code="PROVIDER_FAILURE" * 10,
            last_error="bad\x00failure" + ("x" * 3000),
        )

        assert len(failed.last_error_code) == MAX_ERROR_CODE_LENGTH
        assert "\x00" not in failed.last_error
        assert len(failed.last_error) == MAX_ERROR_LENGTH

    def test_verified_output_identity_is_written_by_terminal_transition(self):
        """Attempt evidence must retain the exact result URI and canonical digest."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.RUNNING)

        completed, _ = transition_execution_attempt(
            attempt.id,
            ExecutionAttemptState.COMPLETED,
            output_envelope_uri="gs://bucket/runs/run-1/output.json",
            output_envelope_sha256="a" * 64,
        )

        assert completed.output_envelope_uri == "gs://bucket/runs/run-1/output.json"
        assert completed.output_envelope_sha256 == "a" * 64

    def test_dispatch_commits_input_digest_and_expected_output_uri(self):
        """Dispatch must persist the contract before any provider can return."""
        attempt = ExecutionAttemptFactory(state=ExecutionAttemptState.PENDING)

        dispatching, _ = transition_execution_attempt(
            attempt.id,
            ExecutionAttemptState.DISPATCHING,
            input_envelope_sha256="a" * 64,
            output_envelope_uri="gs://bucket/runs/run-1/output.json",
        )

        assert dispatching.input_envelope_sha256 == "a" * 64
        assert dispatching.output_envelope_uri == "gs://bucket/runs/run-1/output.json"


@pytest.mark.django_db
class TestProviderExecutionIdentity:
    """Read provider identity only from durable execution-attempt columns."""

    def test_provider_identity_reads_attempt_row(self):
        """Cancellation must use durable columns rather than mutable step JSON."""
        attempt = ExecutionAttemptFactory(
            state=ExecutionAttemptState.RUNNING,
            provider_execution_id="attempt-execution",
            execution_bundle_uri="gs://bucket/attempt",
        )
        attempt.step_run.output = {"execution_name": "stale-legacy-execution"}
        attempt.step_run.save(update_fields=["output"])

        identity = resolve_provider_execution_identity(attempt.step_run)

        assert identity is not None
        assert identity.execution_id == "attempt-execution"
        assert identity.execution_bundle_uri == "gs://bucket/attempt"
        assert identity.attempt == attempt

    def test_provider_identity_never_falls_back_to_step_output(self):
        """Missing attempt identity must not permit stale JSON fallback."""
        step_run = ValidationStepRunFactory(
            output={"execution_name": "stale-legacy-execution"},
        )

        assert resolve_provider_execution_identity(step_run) is None
        assert get_active_execution_attempt(step_run) is None
