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
    snapshot = (
        attempt.deployment_snapshot
        if attempt and isinstance(attempt.deployment_snapshot, dict)
        else {}
    )
    deployment = attempt.deployment if attempt and attempt.deployment_id else None
    provider_type = snapshot.get("provider_type")
    if not provider_type and deployment is not None:
        provider_type = deployment.provider_type
    deployment_kind = snapshot.get("deployment_kind")
    if not deployment_kind and deployment is not None:
        deployment_kind = deployment.deployment_kind
    return {
        "run_id": str(run.pk),
        "step_run_id": step_run.pk if step_run else None,
        "attempt_id": str(attempt.pk) if attempt else None,
        "runner_type": attempt.runner_type if attempt else None,
        "execution_deployment_id": (
            str(snapshot.get("deployment_id") or attempt.deployment_id)
            if attempt and (snapshot.get("deployment_id") or attempt.deployment_id)
            else None
        ),
        "execution_provider_type": (
            str(provider_type) if attempt and provider_type else None
        ),
        "execution_deployment_kind": (
            str(deployment_kind) if attempt and deployment_kind else None
        ),
        "provider_resource_name": (
            str(
                snapshot.get("provider_resource_name") or attempt.provider_resource_name
            )
            if attempt
            else None
        ),
        "provider_execution_id": provider_execution_id
        or (attempt.provider_execution_id if attempt else None),
    }
