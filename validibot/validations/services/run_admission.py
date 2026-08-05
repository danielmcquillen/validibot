"""Race-safe admission and definition-release lifecycle for validation runs.

Every production run creator must enter through :func:`admit_validation_run`.
Admission locks the workflow before the submission so it serializes with
editing-policy transitions and Mutable semantic mutations. The run remains an
active definition user until :func:`emit_validation_run_finalized` has invoked
all synchronous finalization receivers and then durably releases the fence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django.db import transaction
from django.utils import timezone

from validibot.validations.models import ValidationRun

if TYPE_CHECKING:
    from collections.abc import Callable

    from validibot.submissions.models import Submission
    from validibot.users.models import Organization
    from validibot.users.models import User
    from validibot.workflows.models import Workflow


@transaction.atomic
def admit_validation_run(
    *,
    org: Organization,
    workflow: Workflow,
    submission: Submission,
    user: User | None,
    source: str,
    extra: dict[str, Any] | None = None,
) -> ValidationRun:
    """Create one unreleased run under the canonical lock order.

    The workflow lock is acquired before the submission lock. Callers may wrap
    this function in a larger transaction for payment or metering work; Django's
    nested transaction keeps both locks until the outer transaction commits.

    Args:
        org: Organization that owns the run and workflow.
        workflow: Exact workflow row being launched.
        submission: Submission whose content will be validated.
        user: Attributed launcher, or ``None`` for anonymous paid admission.
        source: Trusted launch-channel value.
        extra: Additional non-reserved ``ValidationRun`` fields.

    Returns:
        The newly-created, unreleased validation run.

    Raises:
        ValueError: If tenant relationships disagree or input was purged.
    """

    from validibot.submissions.models import Submission as SubmissionModel
    from validibot.workflows.models import Workflow as WorkflowModel

    locked_workflow = WorkflowModel.objects.select_for_update().get(pk=workflow.pk)
    if locked_workflow.org_id != org.pk:
        raise ValueError("Run organization must match workflow organization")

    locked_submission = SubmissionModel.objects.select_for_update().get(
        pk=submission.pk,
    )
    if locked_submission.workflow_id != locked_workflow.pk:
        raise ValueError("Submission must match the admitted workflow")
    if locked_submission.org_id != org.pk:
        raise ValueError("Run organization must match submission organization")
    if locked_submission.content_purged_at:
        raise ValueError("Submission content is no longer available for validation")

    run_extra = dict(extra or {})
    reserved_fields = {
        "definition_released_at",
        "org",
        "project",
        "source",
        "status",
        "submission",
        "user",
        "workflow",
    }
    conflicts = reserved_fields.intersection(run_extra)
    if conflicts:
        conflict_names = ", ".join(sorted(conflicts))
        raise ValueError(f"Run admission received reserved fields: {conflict_names}")

    from validibot.validations.constants import ValidationRunStatus

    return ValidationRun.objects.create(
        org=org,
        workflow=locked_workflow,
        submission=locked_submission,
        project=locked_submission.project or locked_workflow.project,
        user=user,
        status=ValidationRunStatus.PENDING,
        source=source,
        output_retention_policy=locked_workflow.output_retention,
        definition_released_at=None,
        **run_extra,
    )


def release_validation_run_definition(validation_run: ValidationRun) -> bool:
    """Idempotently mark a run as no longer reading its workflow definition."""

    released_at = timezone.now()
    updated = ValidationRun.objects.filter(
        pk=validation_run.pk,
        definition_released_at__isnull=True,
    ).update(definition_released_at=released_at)
    if updated:
        validation_run.definition_released_at = released_at
    elif validation_run.definition_released_at is None:
        validation_run.refresh_from_db(fields=["definition_released_at"])
    return bool(updated)


def emit_validation_run_finalized(
    *,
    sender: object,
    validation_run: ValidationRun,
) -> list[tuple[Callable[..., Any], Any]]:
    """Notify finalizers, then release the run's workflow-definition fence.

    Django's ``send_robust`` invokes receivers synchronously. Releasing only
    after it returns ensures evidence, retention preparation, credentials, and
    commercial lifecycle receivers have finished any live-definition reads.
    """

    from validibot.validations.signals import validation_run_finalized

    try:
        return validation_run_finalized.send_robust(
            sender=sender,
            validation_run=validation_run,
        )
    finally:
        release_validation_run_definition(validation_run)
