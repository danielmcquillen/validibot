"""Allocation, lookup, and monotonic transitions for execution attempts.

Keeping the attempt aggregate's small transition graph and provider-identity
lookup in one module prevents callbacks, cancellation, reconciliation, and
dispatch code from inventing subtly different lifecycle rules.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from dataclasses import field
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.crypto import salted_hmac
from validibot_shared.canonicalization import compute_callback_nonce_commitment

from validibot.core.textsafety import sanitize_plain_text
from validibot.validations.constants import EXECUTION_ATTEMPT_ACTIVE_STATES
from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import ValidatorExecutionProfile
from validibot.validations.services.execution_logging import execution_log_context

if TYPE_CHECKING:
    from datetime import datetime

    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationStepRun
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)


class InvalidExecutionAttemptTransitionError(ValueError):
    """A requested attempt state change violates the monotonic graph."""


class CallbackCredentialsAlreadyIssuedError(RuntimeError):
    """Callback credentials were already committed for this attempt."""


@dataclass(frozen=True, slots=True)
class ProviderExecutionIdentity:
    """Provider address and workspace resolved from durable attempt storage."""

    execution_id: str
    execution_bundle_uri: str
    runner_type: str
    attempt: ExecutionAttempt


@dataclass(frozen=True, slots=True)
class AttemptCallbackCredentials:
    """One-time callback values placed in an async attempt envelope.

    The raw nonce is deliberately returned only to the caller constructing the
    input envelope. Only its keyed verifier is persisted on ``ExecutionAttempt``.
    """

    callback_id: str
    callback_nonce: str = field(repr=False)
    callback_nonce_commitment: str


ATTEMPT_CALLBACK_PREFIX = "execution-attempt-"
CALLBACK_NONCE_VERIFIER_PREFIX = "hmac-sha256$"
_CALLBACK_NONCE_HMAC_SALT = "validibot.validations.callback-nonce.v1"
_CALLBACK_NONCE_BYTES = 32


def build_attempt_callback_id(attempt: ExecutionAttempt) -> str:
    """Return the opaque callback id that binds delivery to one attempt."""
    return f"{ATTEMPT_CALLBACK_PREFIX}{attempt.pk}"


def build_callback_nonce_verifier(callback_nonce: str) -> str:
    """Build the keyed verifier stored for a raw callback nonce.

    This verifier is intentionally distinct from the public commitment in the
    canonical input envelope. The HMAC prevents a database-only compromise
    from supplying a valid verifier for an attacker-chosen callback nonce.
    """
    if not callback_nonce:
        msg = "Callback nonce cannot be empty"
        raise ValueError(msg)
    digest = salted_hmac(
        _CALLBACK_NONCE_HMAC_SALT,
        callback_nonce,
        algorithm="sha256",
    ).hexdigest()
    return f"{CALLBACK_NONCE_VERIFIER_PREFIX}{digest}"


def verify_attempt_callback_nonce(
    attempt: ExecutionAttempt,
    callback_nonce: str | None,
) -> bool:
    """Verify a callback nonce against an attempt without exposing its secret."""
    stored_verifier = attempt.callback_nonce_hash
    if not callback_nonce or not stored_verifier.startswith(
        CALLBACK_NONCE_VERIFIER_PREFIX,
    ):
        return False
    expected = build_callback_nonce_verifier(callback_nonce)
    return hmac.compare_digest(stored_verifier, expected)


def issue_attempt_callback_credentials(
    attempt: ExecutionAttempt,
) -> AttemptCallbackCredentials:
    """Issue and durably bind one callback secret to an async attempt.

    Issuance is a one-time operation. A second caller cannot rotate the nonce
    after an input envelope may already have been materialized for this
    attempt; it must converge on the existing launch or allocate a new attempt.
    """
    from validibot.validations.models import ExecutionAttempt

    callback_nonce = secrets.token_urlsafe(_CALLBACK_NONCE_BYTES)
    callback_nonce_hash = build_callback_nonce_verifier(callback_nonce)
    updated = ExecutionAttempt.objects.filter(
        pk=attempt.pk,
        callback_nonce_hash="",
    ).update(callback_nonce_hash=callback_nonce_hash)
    if updated != 1:
        raise CallbackCredentialsAlreadyIssuedError(
            f"Execution attempt {attempt.pk} already has callback credentials",
        )
    attempt.callback_nonce_hash = callback_nonce_hash
    return AttemptCallbackCredentials(
        callback_id=build_attempt_callback_id(attempt),
        callback_nonce=callback_nonce,
        callback_nonce_commitment=compute_callback_nonce_commitment(callback_nonce),
    )


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
    runner_type: str | None = None,
    validator: Validator | None = None,
    managed: bool = False,
    effective_budget_seconds: int | None = None,
    execution_profile: ValidatorExecutionProfile | str = (
        ValidatorExecutionProfile.FAST_RESPONSE
    ),
) -> tuple[ExecutionAttempt, bool]:
    """Return the active attempt, creating it before provider work begins.

    Locking the logical step makes attempt-number allocation deterministic and
    complements the partial unique constraint that permits only one active
    attempt.
    """
    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationStepRun
    from validibot.validations.services.execution.deployments import (
        build_deployment_snapshot,
    )
    from validibot.validations.services.execution.deployments import (
        resolve_execution_deployment,
    )
    from validibot.validations.services.execution.registry import (
        get_managed_execution_backend_route,
    )

    budget_seconds = (
        effective_budget_seconds
        if effective_budget_seconds is not None
        else int(getattr(settings, "VALIDATOR_TIMEOUT_SECONDS", 3600))
    )
    if budget_seconds < 1:
        msg = "The effective execution budget must be at least one second."
        raise ValueError(msg)
    try:
        requested_profile = ValidatorExecutionProfile(execution_profile)
    except ValueError as exc:
        msg = f"Unknown validator execution profile: {execution_profile!r}."
        raise ValueError(msg) from exc

    with transaction.atomic():
        locked_step = (
            ValidationStepRun.objects.select_for_update()
            .select_related("validation_run")
            .get(pk=step_run.pk)
        )
        active = get_active_execution_attempt(locked_step, for_update=True)
        if active is not None:
            return active, False

        last_number = (
            locked_step.execution_attempts.aggregate(value=Max("attempt_number"))[
                "value"
            ]
            or 0
        )
        deployment = None
        deployment_snapshot: dict[str, object] = {}
        resolved_runner_type = runner_type
        if managed:
            if validator is None:
                msg = "Managed execution attempt allocation requires a validator."
                raise ValueError(msg)
            deployment = resolve_execution_deployment(
                validator=validator,
                effective_budget_seconds=budget_seconds,
                execution_profile=requested_profile,
                for_update=True,
            )
            deployment_snapshot = build_deployment_snapshot(deployment)
            resolved_runner_type = get_managed_execution_backend_route(
                deployment
            ).runner_type
        if not resolved_runner_type:
            msg = "Execution attempt allocation requires a runner type."
            raise ValueError(msg)
        now = timezone.now()
        attempt_deadline_seconds = budget_seconds
        if deployment is not None:
            # Provider execution budgets cover domain work. The durable attempt
            # deadline also leaves bounded startup/callback room; Services use
            # their verified request timeout as the exact outer request bound.
            if deployment.request_timeout_seconds is not None:
                attempt_deadline_seconds = max(
                    budget_seconds,
                    deployment.request_timeout_seconds,
                )
            else:
                attempt_deadline_seconds = budget_seconds + 120
        attempt = ExecutionAttempt.objects.create(
            step_run=locked_step,
            attempt_number=last_number + 1,
            runner_type=resolved_runner_type[:64],
            deployment=deployment,
            deployment_snapshot=deployment_snapshot,
            provider_resource_name=(
                deployment.provider_resource_name if deployment is not None else ""
            ),
            backend_image_ref=(
                deployment.backend_image_ref if deployment is not None else ""
            ),
            backend_image_digest=(
                deployment.backend_image_digest if deployment is not None else ""
            ),
            timeout_at=now + timedelta(seconds=attempt_deadline_seconds),
            retry_policy_snapshot={
                "schema_version": 2,
                "effective_budget_seconds": budget_seconds,
                "requested_execution_profile": requested_profile.value,
                "attempt_deadline_seconds": attempt_deadline_seconds,
                "maximum_provider_dispatches": 1,
                "provider_acceptance_policy": (
                    "deterministic_same_task_only_after_claim"
                ),
            },
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


def record_execution_attempt_backend_image_digest(
    *,
    step_run_id: UUID | str,
    provider_execution_id: str,
    backend_image_digest: str,
) -> bool:
    """Bind a resolved provider image digest to its concrete attempt once.

    Cloud Run exposes the resolved image only after dispatch returns its
    provider execution id. Matching on both the logical step and that provider
    id avoids copying a later retry's image identity onto an earlier attempt.

    Returns:
        ``True`` when the digest was first persisted, otherwise ``False``.
    """
    from validibot.validations.models import ExecutionAttempt

    if not provider_execution_id or not backend_image_digest:
        return False
    normalized_digest = backend_image_digest[:256]
    with transaction.atomic():
        attempt = (
            ExecutionAttempt.objects.select_for_update()
            .filter(
                step_run_id=step_run_id,
                provider_execution_id=provider_execution_id,
            )
            .first()
        )
        if attempt is None:
            logger.warning(
                "Could not bind backend image digest: provider execution %s "
                "was not found for step run %s",
                provider_execution_id,
                step_run_id,
            )
            return False
        if attempt.backend_image_digest:
            if attempt.backend_image_digest != normalized_digest:
                logger.error(
                    "Refused conflicting backend image digest for execution attempt %s",
                    attempt.pk,
                )
            return False
        attempt.backend_image_digest = normalized_digest
        attempt.save(update_fields=["backend_image_digest", "modified"])
    return True


def transition_execution_attempt(
    attempt_id: UUID | str,
    target: str | ExecutionAttemptState,
    *,
    provider_status_code: str | None = None,
    last_error_code: str | None = None,
    last_error: str | None = None,
    provider_accepted_at: datetime | None = None,
    provider_started_at: datetime | None = None,
    provider_finished_at: datetime | None = None,
    callback_received_at: datetime | None = None,
    provider_resource_name: str | None = None,
    provider_execution_id: str | None = None,
    execution_bundle_uri: str | None = None,
    input_envelope_uri: str | None = None,
    input_envelope_sha256: str | None = None,
    input_evidence_snapshot: dict | None = None,
    output_envelope_uri: str | None = None,
    output_envelope_sha256: str | None = None,
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
            "provider_accepted_at": provider_accepted_at,
            "provider_started_at": provider_started_at,
            "provider_finished_at": provider_finished_at,
            "callback_received_at": callback_received_at,
            "provider_resource_name": (
                provider_resource_name[:512]
                if provider_resource_name is not None
                else None
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
            "input_envelope_sha256": (
                input_envelope_sha256[:64]
                if input_envelope_sha256 is not None
                else None
            ),
            "input_evidence_snapshot": (
                dict(input_evidence_snapshot)
                if input_evidence_snapshot is not None
                else None
            ),
            "output_envelope_uri": (
                output_envelope_uri[:2048] if output_envelope_uri is not None else None
            ),
            "output_envelope_sha256": (
                output_envelope_sha256[:64]
                if output_envelope_sha256 is not None
                else None
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
