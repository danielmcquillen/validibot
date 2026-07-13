"""Allocation, lookup, and monotonic transitions for execution attempts.

Keeping the attempt aggregate's small transition graph and provider-identity
lookup in one module prevents callbacks, cancellation, reconciliation, and
dispatch code from inventing subtly different lifecycle rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from validibot.core.textsafety import sanitize_plain_text
from validibot.validations.constants import EXECUTION_ATTEMPT_ACTIVE_STATES
from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.services.runtime_profiles import execution_log_context
from validibot.validations.services.runtime_profiles import get_runtime_profile_policy

if TYPE_CHECKING:
    from datetime import datetime

    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationStepRun

logger = logging.getLogger(__name__)


class InvalidExecutionAttemptTransitionError(ValueError):
    """A requested attempt state change violates the monotonic graph."""


@dataclass(frozen=True, slots=True)
class ProviderExecutionIdentity:
    """Provider address and workspace resolved from durable attempt storage."""

    execution_id: str
    execution_bundle_uri: str
    runner_type: str
    attempt: ExecutionAttempt


ATTEMPT_CALLBACK_PREFIX = "execution-attempt-"


def build_attempt_callback_id(attempt: ExecutionAttempt) -> str:
    """Return the opaque callback id that binds delivery to one attempt."""
    return f"{ATTEMPT_CALLBACK_PREFIX}{attempt.pk}"


def resolve_callback_attempt(
    callback_id: str | None,
    *,
    run_id: UUID | str,
) -> ExecutionAttempt | None:
    """Resolve an attempt-bound callback without trusting its run identifier.

    The callback id is an opaque idempotency key in the shared envelope
    contract. Attempt-mode writers encode the attempt UUID in that key, while
    this reader verifies the database relationship back to ``run_id``.
    """
    from validibot.validations.models import ExecutionAttempt

    if not callback_id or not callback_id.startswith(ATTEMPT_CALLBACK_PREFIX):
        return None
    raw_attempt_id = callback_id.removeprefix(ATTEMPT_CALLBACK_PREFIX)
    try:
        attempt_id = UUID(raw_attempt_id)
    except ValueError:
        return None
    return (
        ExecutionAttempt.objects.select_related("step_run__validation_run")
        .filter(pk=attempt_id, step_run__validation_run_id=run_id)
        .first()
    )


def get_or_create_execution_attempt(
    step_run: ValidationStepRun,
    *,
    runner_type: str,
) -> tuple[ExecutionAttempt, bool]:
    """Return the active attempt, creating it before provider work begins.

    Locking the logical step makes attempt-number allocation deterministic and
    complements the partial unique constraint that permits only one active
    attempt.
    """
    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationStepRun

    with transaction.atomic():
        locked_step = (
            ValidationStepRun.objects.select_for_update()
            .select_related("validation_run")
            .get(pk=step_run.pk)
        )
        policy = get_runtime_profile_policy(locked_step.validation_run.runtime_profile)

        active = get_active_execution_attempt(locked_step, for_update=True)
        if active is not None:
            return active, False

        last_number = (
            locked_step.execution_attempts.aggregate(value=Max("attempt_number"))[
                "value"
            ]
            or 0
        )
        attempt = ExecutionAttempt.objects.create(
            step_run=locked_step,
            attempt_number=last_number + 1,
            runner_type=runner_type[:64],
            contract_version=policy.contract_version,
        )
        return attempt, True


_ALLOWED_TRANSITIONS = {
    ExecutionAttemptState.PENDING: frozenset(
        {
            ExecutionAttemptState.DISPATCHING,
            ExecutionAttemptState.FAILED,
            ExecutionAttemptState.CANCELED,
        }
    ),
    ExecutionAttemptState.DISPATCHING: frozenset(
        {
            ExecutionAttemptState.RUNNING,
            ExecutionAttemptState.UNKNOWN,
            ExecutionAttemptState.FAILED,
            ExecutionAttemptState.CANCELED,
            ExecutionAttemptState.TIMED_OUT,
        }
    ),
    ExecutionAttemptState.RUNNING: frozenset(
        {
            ExecutionAttemptState.COMPLETED,
            ExecutionAttemptState.FAILED,
            ExecutionAttemptState.CANCELED,
            ExecutionAttemptState.TIMED_OUT,
        }
    ),
    ExecutionAttemptState.UNKNOWN: frozenset(
        {
            ExecutionAttemptState.RUNNING,
            ExecutionAttemptState.COMPLETED,
            ExecutionAttemptState.FAILED,
            ExecutionAttemptState.CANCELED,
            ExecutionAttemptState.TIMED_OUT,
        }
    ),
    ExecutionAttemptState.COMPLETED: frozenset(),
    ExecutionAttemptState.FAILED: frozenset(),
    ExecutionAttemptState.CANCELED: frozenset(),
    ExecutionAttemptState.TIMED_OUT: frozenset(),
}


def is_attempt_transition_allowed(
    current: str | ExecutionAttemptState,
    target: str | ExecutionAttemptState,
) -> bool:
    """Return whether ``current`` may move to ``target`` or replay itself."""
    try:
        current_state = ExecutionAttemptState(current)
        target_state = ExecutionAttemptState(target)
    except ValueError:
        return False
    return (
        target_state == current_state
        or target_state in _ALLOWED_TRANSITIONS[current_state]
    )


def get_active_execution_attempt(
    step_run: ValidationStepRun,
    *,
    for_update: bool = False,
) -> ExecutionAttempt | None:
    """Return the one non-terminal attempt for a step, if one exists."""
    queryset = step_run.execution_attempts.filter(
        state__in=EXECUTION_ATTEMPT_ACTIVE_STATES
    ).order_by("-attempt_number")
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.first()


def resolve_provider_execution_identity(
    step_run: ValidationStepRun,
) -> ProviderExecutionIdentity | None:
    """Read provider identity exclusively from durable attempt storage."""
    attempt = get_active_execution_attempt(step_run)
    if attempt is None:
        # Terminal fencing deliberately happens before best-effort provider
        # cancellation. Retain the latest attempt's provider address so the
        # external cancel request can run after the DB commit.
        attempt = step_run.execution_attempts.order_by("-attempt_number").first()
    if attempt is None or not attempt.provider_execution_id:
        return None
    return ProviderExecutionIdentity(
        execution_id=attempt.provider_execution_id,
        execution_bundle_uri=attempt.execution_bundle_uri,
        runner_type=attempt.runner_type,
        attempt=attempt,
    )


def transition_execution_attempt(
    attempt_id: UUID | str,
    target: str | ExecutionAttemptState,
    *,
    provider_status_code: str | None = None,
    last_error_code: str | None = None,
    last_error: str | None = None,
    provider_started_at: datetime | None = None,
    provider_finished_at: datetime | None = None,
    provider_job_name: str | None = None,
    provider_execution_id: str | None = None,
    execution_bundle_uri: str | None = None,
    input_envelope_uri: str | None = None,
    backend_image_digest: str | None = None,
) -> tuple[ExecutionAttempt, bool]:
    """Lock and monotonically transition one attempt.

    Same-state delivery is an idempotent no-op.  Terminal attempts never
    reopen.  Provider observations are written only with a real state change;
    they are bounded and remain diagnostics rather than transition authority.

    Returns:
        ``(attempt, changed)`` with the refreshed locked model.

    Raises:
        InvalidExecutionAttemptTransitionError: If the requested edge is illegal.
        ExecutionAttempt.DoesNotExist: If ``attempt_id`` is unknown.
    """
    from validibot.validations.models import ExecutionAttempt

    try:
        target_state = ExecutionAttemptState(target)
    except ValueError as exc:
        raise InvalidExecutionAttemptTransitionError(
            f"Unknown execution attempt target state: {target!r}"
        ) from exc

    with transaction.atomic():
        attempt = (
            ExecutionAttempt.objects.select_for_update()
            .select_related("step_run__validation_run")
            .get(pk=attempt_id)
        )
        try:
            current_state = ExecutionAttemptState(attempt.state)
        except ValueError as exc:
            raise InvalidExecutionAttemptTransitionError(
                f"Attempt {attempt.pk} has unknown state {attempt.state!r}"
            ) from exc

        if target_state == current_state:
            return attempt, False
        if target_state not in _ALLOWED_TRANSITIONS[current_state]:
            raise InvalidExecutionAttemptTransitionError(
                f"Execution attempt cannot transition from {current_state} "
                f"to {target_state}"
            )

        attempt.state = target_state
        update_fields = ["state", "modified"]
        if target_state == ExecutionAttemptState.DISPATCHING:
            attempt.dispatch_started_at = attempt.dispatch_started_at or timezone.now()
            update_fields.append("dispatch_started_at")
        if target_state in EXECUTION_ATTEMPT_TERMINAL_STATES:
            attempt.terminal_at = attempt.terminal_at or timezone.now()
            update_fields.append("terminal_at")

        optional_updates = {
            "provider_status_code": (
                provider_status_code[:64] if provider_status_code is not None else None
            ),
            "last_error_code": (
                last_error_code[:64] if last_error_code is not None else None
            ),
            "last_error": (
                sanitize_plain_text(last_error)[:2000]
                if last_error is not None
                else None
            ),
            "provider_started_at": provider_started_at,
            "provider_finished_at": provider_finished_at,
            "provider_job_name": (
                provider_job_name[:512] if provider_job_name is not None else None
            ),
            "provider_execution_id": (
                provider_execution_id[:512]
                if provider_execution_id is not None
                else None
            ),
            "execution_bundle_uri": (
                execution_bundle_uri[:2048]
                if execution_bundle_uri is not None
                else None
            ),
            "input_envelope_uri": (
                input_envelope_uri[:2048] if input_envelope_uri is not None else None
            ),
            "backend_image_digest": (
                backend_image_digest[:256] if backend_image_digest is not None else None
            ),
        }
        for field_name, value in optional_updates.items():
            if value is not None:
                setattr(attempt, field_name, value)
                update_fields.append(field_name)

        attempt.save(update_fields=update_fields)

    logger.info(
        "Transitioned execution attempt from %s to %s",
        current_state,
        target_state,
        extra=execution_log_context(
            attempt.step_run.validation_run,
            step_run=attempt.step_run,
            attempt=attempt,
        ),
    )
    return attempt, True
