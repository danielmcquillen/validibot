"""Tests for race-safe validation-run admission and definition release.

The marker is a durable ownership record, not a status approximation. These
tests prove every admitted run fences its live workflow until synchronous
finalizers have had their last opportunity to read the definition.
"""

from __future__ import annotations

import pytest
from django.dispatch import receiver

from validibot.submissions.models import Submission
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import ValidationRunSource
from validibot.validations.services.run_admission import admit_validation_run
from validibot.validations.services.run_admission import emit_validation_run_finalized
from validibot.validations.services.run_admission import (
    release_validation_run_definition,
)
from validibot.validations.signals import validation_run_finalized
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def test_admission_creates_an_unreleased_definition_user():
    """A newly queued run must fence Mutable edits before workers can start."""

    workflow = WorkflowFactory()
    submission = SubmissionFactory(workflow=workflow)

    run = admit_validation_run(
        org=workflow.org,
        workflow=workflow,
        submission=submission,
        user=workflow.user,
        source=ValidationRunSource.LAUNCH_PAGE,
    )

    assert run.definition_released_at is None
    assert run.workflow_id == workflow.pk
    assert run.project_id == submission.project_id
    assert run.output_retention_policy == workflow.output_retention


def test_admission_rejects_cross_workflow_submission():
    """The canonical lock path must not accidentally join unrelated tenants."""

    workflow = WorkflowFactory()
    other_submission = SubmissionFactory()

    with pytest.raises(ValueError, match="Submission must match"):
        admit_validation_run(
            org=workflow.org,
            workflow=workflow,
            submission=other_submission,
            user=workflow.user,
            source=ValidationRunSource.LAUNCH_PAGE,
        )


def test_admission_rejects_cross_organization_workflow():
    """Reloading under lock must preserve the existing tenant invariant."""

    workflow = WorkflowFactory()
    submission = SubmissionFactory(workflow=workflow)

    with pytest.raises(ValueError, match="organization must match"):
        admit_validation_run(
            org=OrganizationFactory(),
            workflow=workflow,
            submission=submission,
            user=workflow.user,
            source=ValidationRunSource.LAUNCH_PAGE,
        )


def test_admission_rejects_internally_inconsistent_submission_tenant():
    """The canonical boundary rechecks tenant ownership even after raw drift."""

    workflow = WorkflowFactory()
    submission = SubmissionFactory(workflow=workflow)
    Submission.objects.filter(pk=submission.pk).update(org=OrganizationFactory())

    with pytest.raises(ValueError, match="submission organization"):
        admit_validation_run(
            org=workflow.org,
            workflow=workflow,
            submission=submission,
            user=workflow.user,
            source=ValidationRunSource.LAUNCH_PAGE,
        )


def test_release_is_idempotent():
    """Duplicate callbacks and cleanup attempts must converge on one release."""

    workflow = WorkflowFactory()
    submission = SubmissionFactory(workflow=workflow)
    run = admit_validation_run(
        org=workflow.org,
        workflow=workflow,
        submission=submission,
        user=workflow.user,
        source=ValidationRunSource.LAUNCH_PAGE,
    )

    assert release_validation_run_definition(run) is True
    first_release = run.definition_released_at
    assert release_validation_run_definition(run) is False

    run.refresh_from_db()
    assert run.definition_released_at == first_release


def test_finalizers_observe_unreleased_marker_before_release():
    """Evidence and credential receivers get the final live-definition window."""

    workflow = WorkflowFactory()
    submission = SubmissionFactory(workflow=workflow)
    run = admit_validation_run(
        org=workflow.org,
        workflow=workflow,
        submission=submission,
        user=workflow.user,
        source=ValidationRunSource.LAUNCH_PAGE,
    )
    observed: list[object] = []

    @receiver(validation_run_finalized, weak=False)
    def observe_release_marker(sender, validation_run, **kwargs):
        """Capture the durable marker at synchronous receiver execution time."""

        validation_run.refresh_from_db(fields=["definition_released_at"])
        observed.append(validation_run.definition_released_at)

    try:
        emit_validation_run_finalized(sender=object(), validation_run=run)
    finally:
        validation_run_finalized.disconnect(observe_release_marker)

    run.refresh_from_db()
    assert observed == [None]
    assert run.definition_released_at is not None


def test_robust_receiver_failure_does_not_leave_permanent_fence():
    """One broken observer must not strand a finalized run as a definition user."""

    workflow = WorkflowFactory()
    submission = SubmissionFactory(workflow=workflow)
    run = admit_validation_run(
        org=workflow.org,
        workflow=workflow,
        submission=submission,
        user=workflow.user,
        source=ValidationRunSource.LAUNCH_PAGE,
    )

    @receiver(validation_run_finalized, weak=False)
    def fail_receiver(sender, **kwargs):
        """Represent a non-critical robust-signal receiver failure."""

        raise RuntimeError("receiver failed")

    try:
        responses = emit_validation_run_finalized(
            sender=object(),
            validation_run=run,
        )
    finally:
        validation_run_finalized.disconnect(fail_receiver)

    run.refresh_from_db()
    assert any(isinstance(response, RuntimeError) for _, response in responses)
    assert run.definition_released_at is not None
