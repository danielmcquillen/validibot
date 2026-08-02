"""Central validation-output retention and purge operations.

Retention is a run-time data-lifecycle contract, not a presentation detail.
This module keeps terminal scheduling, physical deletion, database redaction,
and truthful purge timestamps in one place so synchronous workers, callbacks,
cancellation, watchdog reconciliation, and repair commands cannot drift.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.constants import get_output_retention_timedelta
from validibot.submissions.constants import get_submission_retention_timedelta
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


def schedule_terminal_retention(run: ValidationRun) -> None:
    """Snapshot a terminal run's output deadline and queue input deletion.

    The deadline is based on ``ended_at`` rather than launch time. Unknown
    policies fail closed to ``DO_NOT_STORE``. This operation is idempotent:
    duplicate terminal signals compute the same deadline and cannot extend it.
    Scheduled repair sweeps independently rediscover missed work.
    """

    if run.status not in VALIDATION_RUN_TERMINAL_STATUSES:
        return

    try:
        policy = OutputRetention(run.output_retention_policy)
    except ValueError:
        policy = OutputRetention.DO_NOT_STORE

    retention_delta = get_output_retention_timedelta(policy)
    completed_at = run.ended_at or timezone.now()
    output_expires_at = (
        None if retention_delta is None else completed_at + retention_delta
    )

    update_fields: dict[str, object] = {}
    if run.ended_at is None:
        # Abnormal terminal writers may omit ``ended_at``. Persist the first
        # observed completion boundary so duplicate hooks cannot move a finite
        # deadline forward and DO_NOT_STORE remains discoverable by sweepers.
        update_fields["ended_at"] = completed_at
        run.ended_at = completed_at
    if run.output_retention_policy != policy:
        update_fields["output_retention_policy"] = policy
        run.output_retention_policy = policy
    if run.output_expires_at != output_expires_at:
        update_fields["output_expires_at"] = output_expires_at
        run.output_expires_at = output_expires_at
    if update_fields:
        type(run).objects.filter(pk=run.pk, output_purged_at__isnull=True).update(
            **update_fields,
        )

    schedule_submission_retention(run.submission)


def schedule_submission_retention(submission) -> None:
    """Set the input deadline once every run using the submission is terminal.

    Finite submissions receive a provisional receipt-time deadline when
    created, which also protects abandoned submissions. After execution, the
    author-selected review window starts at the latest related ``ended_at``.
    Shared inputs are never scheduled while a sibling run is active.
    """
    if submission is None or submission.content_purged_at:
        return
    if submission.runs.exclude(
        status__in=VALIDATION_RUN_TERMINAL_STATUSES,
    ).exists():
        return

    try:
        policy = SubmissionRetention(submission.retention_policy)
    except ValueError:
        policy = SubmissionRetention.DO_NOT_STORE

    retention_delta = get_submission_retention_timedelta(policy)
    update_fields: dict[str, object] = {}
    if submission.retention_policy != policy:
        update_fields["retention_policy"] = policy
        submission.retention_policy = policy

    if retention_delta is None or retention_delta.total_seconds() <= 0:
        expires_at = None
    else:
        latest_end = submission.runs.aggregate(latest=Max("ended_at"))["latest"]
        expires_at = (latest_end or submission.created) + retention_delta

    if submission.expires_at != expires_at:
        update_fields["expires_at"] = expires_at
        submission.expires_at = expires_at
    if update_fields:
        type(submission).objects.filter(
            pk=submission.pk,
            content_purged_at__isnull=True,
        ).update(**update_fields)

    if policy == SubmissionRetention.DO_NOT_STORE:
        from validibot.submissions.models import queue_submission_purge

        queue_submission_purge(submission)


@transaction.atomic
def purge_run_outputs(run: ValidationRun) -> ValidationRun:
    """Delete and redact every detailed output for one terminal run.

    The durable remainder includes the permanent evidence manifest plus run
    identity, tenant and workflow links, terminal status/category/timing,
    aggregate counts, step status/count summaries, provider execution identity,
    and purge timestamps. Payload-bearing findings, artifacts, step values,
    detailed errors, envelope locations/digests, and callback result locations
    are removed.

    External deletion failures propagate. The enclosing transaction therefore
    never stamps ``output_purged_at`` while required bytes may remain.
    """

    from validibot.submissions.models import _delete_run_files
    from validibot.validations.models import Artifact
    from validibot.validations.models import CallbackReceipt
    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationFinding
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationRunSummary
    from validibot.validations.models import ValidationStepRun

    locked_run = ValidationRun.objects.select_for_update().get(pk=run.pk)
    if locked_run.output_purged_at:
        return locked_run
    if locked_run.status not in VALIDATION_RUN_TERMINAL_STATUSES:
        msg = f"Cannot purge outputs for non-terminal run {locked_run.pk}"
        raise ValueError(msg)

    run_id = str(locked_run.id)

    # Delete externally stored artifact files before discarding their durable
    # retry identities. URI-backed artifacts live in the run bundle deleted
    # below; FileField-backed artifacts need an explicit storage delete.
    artifacts = list(locked_run.artifacts.all())
    for artifact in artifacts:
        if artifact.file:
            try:
                artifact.file.delete(save=False)
            except Exception:
                logger.exception(
                    "Failed to delete artifact file",
                    extra={"run_id": run_id, "artifact_id": artifact.pk},
                )
                raise

    # The execution bundle contains input/output envelopes, copied submission
    # bytes, execution-time manifests, and validator-produced files. It is a
    # required part of both input and output deletion truth. The permanent
    # evidence receipt uses a separate ``evidence/`` storage prefix and is not
    # deleted here.
    _delete_run_files(locked_run)

    findings_count = locked_run.findings.count()
    ValidationFinding.objects.filter(validation_run=locked_run).delete()
    Artifact.objects.filter(validation_run=locked_run).delete()

    ValidationStepRun.objects.filter(validation_run=locked_run).update(
        output={},
        input_values={},
        output_values={},
        error="",
    )
    ExecutionAttempt.objects.filter(step_run__validation_run=locked_run).update(
        execution_bundle_uri="",
        input_envelope_uri="",
        output_envelope_uri="",
        output_envelope_sha256="",
        last_error="",
    )
    CallbackReceipt.objects.filter(validation_run=locked_run).update(result_uri="")
    ValidationRunSummary.objects.filter(run=locked_run).update(extras={})

    locked_run.error = ""
    locked_run.output_hash = ""
    locked_run.output_purged_at = timezone.now()
    locked_run.output_expires_at = None
    locked_run.save(
        update_fields=[
            "error",
            "output_hash",
            "output_purged_at",
            "output_expires_at",
            "modified",
        ],
    )

    logger.info(
        "Purged detailed validation outputs",
        extra={
            "run_id": run_id,
            "output_retention_policy": locked_run.output_retention_policy,
            "findings_deleted": findings_count,
            "artifacts_deleted": len(artifacts),
        },
    )
    return locked_run


def redact_run_input_records(run: ValidationRun) -> None:
    """Remove payload-derived input context after submission bytes are deleted.

    Input checksums and server-derived size/type facts remain as minimal
    integrity evidence. Submitter-provided labels, filenames, metadata, step
    input values, and input-envelope storage identities do not.
    """

    from validibot.validations.models import ExecutionAttempt
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun

    ValidationRun.objects.filter(pk=run.pk).update(short_description="")
    ValidationStepRun.objects.filter(validation_run=run).update(input_values={})
    ExecutionAttempt.objects.filter(step_run__validation_run=run).update(
        execution_bundle_uri="",
        input_envelope_uri="",
        input_envelope_sha256="",
        input_evidence_snapshot={},
    )


__all__ = [
    "purge_run_outputs",
    "redact_run_input_records",
    "schedule_submission_retention",
    "schedule_terminal_retention",
]
