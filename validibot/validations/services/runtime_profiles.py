"""Runtime-profile policy and mixed-version execution guards.

The validation-execution integrity program introduces execution behavior in
reader-first releases.  This module is the single table that explains each
accepted profile and the guard used by legacy handlers until attempt writers
are enabled.  It deliberately avoids feature-flag inference: the immutable
value stored on each run is authoritative for that run's lifetime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import ExecutionContractVersion
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationRuntimeProfile

if TYPE_CHECKING:
    from collections.abc import Iterable

    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun

logger = logging.getLogger(__name__)

UNSUPPORTED_RUNTIME_PROFILE_ERROR = (
    "This validation run uses execution semantics that this deployment cannot "
    "process safely."
)


class UnsupportedRuntimeProfileError(ValueError):
    """A stored profile is unknown to this release."""


@dataclass(frozen=True, slots=True)
class RuntimeProfilePolicy:
    """Resolved execution capabilities for one immutable runtime profile."""

    profile: ValidationRuntimeProfile
    contract_version: ExecutionContractVersion
    uses_execution_attempts: bool
    uses_strict_io: bool
    uses_canonical_context: bool


_PROFILE_SEQUENCE = (
    ValidationRuntimeProfile.LEGACY,
    ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
    ValidationRuntimeProfile.ATTEMPT_STRICT_V1,
    ValidationRuntimeProfile.ATTEMPT_CONTEXT_V1,
)

_PROFILE_POLICIES = {
    ValidationRuntimeProfile.LEGACY: RuntimeProfilePolicy(
        profile=ValidationRuntimeProfile.LEGACY,
        contract_version=ExecutionContractVersion.LEGACY_URI_V1,
        uses_execution_attempts=False,
        uses_strict_io=False,
        uses_canonical_context=False,
    ),
    ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1: RuntimeProfilePolicy(
        profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
        contract_version=ExecutionContractVersion.LEGACY_URI_V1,
        uses_execution_attempts=True,
        uses_strict_io=False,
        uses_canonical_context=False,
    ),
    ValidationRuntimeProfile.ATTEMPT_STRICT_V1: RuntimeProfilePolicy(
        profile=ValidationRuntimeProfile.ATTEMPT_STRICT_V1,
        contract_version=ExecutionContractVersion.STRICT_CONTENT_V1,
        uses_execution_attempts=True,
        uses_strict_io=True,
        uses_canonical_context=False,
    ),
    ValidationRuntimeProfile.ATTEMPT_CONTEXT_V1: RuntimeProfilePolicy(
        profile=ValidationRuntimeProfile.ATTEMPT_CONTEXT_V1,
        contract_version=ExecutionContractVersion.STRICT_CONTENT_V1,
        uses_execution_attempts=True,
        uses_strict_io=True,
        uses_canonical_context=True,
    ),
}

# Stage 1 is intentionally reader-first.  Later stages expand this set only
# after every callback, task, watchdog, and reconciliation instance can handle
# the next profile.
WRITER_ENABLED_RUNTIME_PROFILES = frozenset(
    {
        ValidationRuntimeProfile.LEGACY,
        ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
    }
)


def get_runtime_profile_policy(
    profile: str | ValidationRuntimeProfile,
) -> RuntimeProfilePolicy:
    """Return the policy for a stored profile, rejecting unknown values.

    Args:
        profile: Database value or ``ValidationRuntimeProfile`` member.

    Raises:
        UnsupportedRuntimeProfileError: If this release does not understand
            the stored value.
    """
    try:
        normalized = ValidationRuntimeProfile(profile)
    except ValueError as exc:
        raise UnsupportedRuntimeProfileError(
            f"Unsupported validation runtime profile: {profile!r}"
        ) from exc
    return _PROFILE_POLICIES[normalized]


def can_advance_runtime_profile(
    current: str | ValidationRuntimeProfile,
    target: str | ValidationRuntimeProfile,
) -> bool:
    """Return whether a deployment default may move to the next profile rung.

    A deployment may remain on its current rung or advance by exactly one.
    Skipping a rung would bypass its mixed-version rollout gate; moving
    backwards could create runs that an older release misinterprets.
    """
    current_policy = get_runtime_profile_policy(current)
    target_policy = get_runtime_profile_policy(target)
    current_index = _PROFILE_SEQUENCE.index(current_policy.profile)
    target_index = _PROFILE_SEQUENCE.index(target_policy.profile)
    return target_index in {current_index, current_index + 1}


def is_runtime_profile_writer_enabled(
    profile: str | ValidationRuntimeProfile,
) -> bool:
    """Return whether this release is allowed to create runs in ``profile``."""
    return (
        get_runtime_profile_policy(profile).profile in WRITER_ENABLED_RUNTIME_PROFILES
    )


def is_runtime_profile_supported(
    profile: str | ValidationRuntimeProfile,
    supported_profiles: Iterable[str | ValidationRuntimeProfile],
) -> bool:
    """Return whether a handler may interpret ``profile`` without mutation."""
    try:
        resolved = get_runtime_profile_policy(profile).profile
        supported = {
            get_runtime_profile_policy(item).profile for item in supported_profiles
        }
    except UnsupportedRuntimeProfileError:
        return False
    return resolved in supported


def execution_log_context(
    run: ValidationRun,
    *,
    step_run: ValidationStepRun | None = None,
    attempt: ExecutionAttempt | None = None,
    provider_execution_id: str | None = None,
) -> dict[str, str | int | None]:
    """Build consistent correlation fields for execution lifecycle logs."""
    return {
        "run_id": str(run.pk),
        "step_run_id": step_run.pk if step_run else None,
        "attempt_id": str(attempt.pk) if attempt else None,
        "runtime_profile": run.runtime_profile,
        "provider_execution_id": provider_execution_id
        or (attempt.provider_execution_id if attempt else None),
    }


def ensure_runtime_profile_supported(
    run: ValidationRun,
    *,
    supported_profiles: Iterable[str | ValidationRuntimeProfile],
    operation: str,
    sender: object,
) -> bool:
    """Fence a run when a handler cannot safely interpret its profile.

    Existing execution handlers call this before reading legacy step-output
    metadata.  During Stage 1 they support ``LEGACY`` only.  If an attempt-mode
    task reaches an old handler during a bad rollout or downgrade, the run is
    terminally failed as a system error rather than silently processed as a
    legacy run.

    Returns:
        ``True`` when the caller may continue, otherwise ``False`` after the
        active run has been fenced.
    """
    if is_runtime_profile_supported(run.runtime_profile, supported_profiles):
        return True

    from validibot.validations.models import ValidationRun

    finalized_run = None
    with transaction.atomic():
        locked_run = ValidationRun.objects.select_for_update().get(pk=run.pk)
        if locked_run.status in VALIDATION_RUN_TERMINAL_STATUSES:
            return False

        ended_at = timezone.now()
        locked_run.status = ValidationRunStatus.FAILED
        locked_run.error_category = ValidationRunErrorCategory.SYSTEM_ERROR
        locked_run.error = UNSUPPORTED_RUNTIME_PROFILE_ERROR
        locked_run.ended_at = ended_at
        if locked_run.started_at:
            locked_run.duration_ms = max(
                int((ended_at - locked_run.started_at).total_seconds() * 1000),
                0,
            )
        locked_run.save(
            update_fields=[
                "status",
                "error_category",
                "error",
                "ended_at",
                "duration_ms",
            ]
        )
        finalized_run = locked_run

    logger.error(
        "Rejected execution operation %s for unsupported runtime profile",
        operation,
        extra=execution_log_context(finalized_run),
    )

    from validibot.validations.signals import validation_run_finalized

    validation_run_finalized.send_robust(
        sender=sender,
        validation_run=finalized_run,
    )
    return False
