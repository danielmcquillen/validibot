"""Reader-first selectors and monotonic transitions for execution attempts.

Stage 1 introduces the attempt aggregate without enabling production attempt
creation.  Keeping its small transition graph and provider-identity lookup in
one module prevents callbacks, cancellation, reconciliation, and future
dispatch code from inventing subtly different lifecycle rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from validibot.core.textsafety import sanitize_plain_text
from validibot.validations.constants import EXECUTION_ATTEMPT_ACTIVE_STATES
from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.services.runtime_profiles import execution_log_context
from validibot.validations.services.runtime_profiles import get_runtime_profile_policy

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationStepRun

logger = logging.getLogger(__name__)


class InvalidExecutionAttemptTransitionError(ValueError):
    """A requested attempt state change violates the monotonic graph."""


@dataclass(frozen=True, slots=True)
class ProviderExecutionIdentity:
    """Provider address and workspace resolved from legacy or attempt storage."""

    execution_id: str
    execution_bundle_uri: str
    runner_type: str
    attempt: ExecutionAttempt | None


_ALLOWED_TRANSITIONS = {
    ExecutionAttemptState.PENDING: frozenset(
        {
            ExecutionAttemptState.DISPATCHING,
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
    """Read provider identity from the source selected by the run profile.

    Legacy runs continue reading coordination metadata from ``step_run.output``.
    Attempt profiles read the active attempt row and never fall back to legacy
    JSON, which prevents mixed-version code from silently using stale identity.
    """
    policy = get_runtime_profile_policy(step_run.validation_run.runtime_profile)
    if policy.uses_execution_attempts:
        attempt = get_active_execution_attempt(step_run)
        if attempt is None or not attempt.provider_execution_id:
            return None
        return ProviderExecutionIdentity(
            execution_id=attempt.provider_execution_id,
            execution_bundle_uri=attempt.execution_bundle_uri,
            runner_type=attempt.runner_type,
            attempt=attempt,
        )

    step_output = step_run.output or {}
    execution_id = step_output.get("execution_name") or step_output.get("execution_id")
    if not execution_id:
        return None
    return ProviderExecutionIdentity(
        execution_id=str(execution_id),
        execution_bundle_uri=str(step_output.get("execution_bundle_uri", "")),
        runner_type="",
        attempt=None,
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
