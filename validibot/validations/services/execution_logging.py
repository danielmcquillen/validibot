"""Structured correlation fields for execution lifecycle logs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun


def execution_log_context(
    run: ValidationRun,
    *,
    step_run: ValidationStepRun | None = None,
    attempt: ExecutionAttempt | None = None,
    provider_execution_id: str | None = None,
) -> dict[str, str | int | None]:
    """Build consistent identifiers for one run, step, and provider launch."""
    return {
        "run_id": str(run.pk),
        "step_run_id": step_run.pk if step_run else None,
        "attempt_id": str(attempt.pk) if attempt else None,
        "provider_execution_id": provider_execution_id
        or (attempt.provider_execution_id if attempt else None),
    }
