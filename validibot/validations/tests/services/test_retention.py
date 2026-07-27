"""Tests for terminal retention scheduling and privacy-safe defaults.

These tests pin the lifecycle boundary that every terminal path shares:
deadlines start at completion, unknown policies fail closed, permanent
retention remains explicit, and a missed caller-specific cleanup hook is
replaced by the common finalized signal.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.constants import get_output_retention_timedelta
from validibot.submissions.constants import get_submission_retention_timedelta
from validibot.submissions.models import PurgeRetry
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.retention import schedule_terminal_retention
from validibot.validations.signals import validation_run_finalized
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.models import Workflow
from validibot.workflows.tests.factories import WorkflowFactory


def test_unknown_retention_helpers_fail_closed():
    """A corrupt choice must never be interpreted as permanent retention."""

    assert get_submission_retention_timedelta("UNKNOWN") == timedelta(0)
    assert get_output_retention_timedelta("UNKNOWN") == timedelta(0)


@pytest.mark.django_db
class TestPrivacySafeDefaults:
    """New authoring and run records must opt in before retaining data."""

    def test_workflow_defaults_both_streams_to_no_retention(self):
        """An author who does nothing must not silently retain payload data."""

        workflow = WorkflowFactory()

        assert workflow.input_retention == SubmissionRetention.DO_NOT_STORE
        assert workflow.output_retention == OutputRetention.DO_NOT_STORE
        assert (
            Workflow._meta.get_field("output_retention").default
            == OutputRetention.DO_NOT_STORE
        )

    def test_run_default_is_no_output_retention(self):
        """Programmatic run creation must share the workflow's safe default."""

        run = ValidationRunFactory()

        assert run.output_retention_policy == OutputRetention.DO_NOT_STORE

    def test_unknown_policies_fail_closed_at_read_time(self):
        """Corrupt policy rows must not expose input or output before repair."""

        run = ValidationRunFactory(
            output_retention_policy="UNKNOWN",
            output_expires_at=None,
        )
        type(run.submission).objects.filter(pk=run.submission_id).update(
            retention_policy="UNKNOWN",
        )
        run.submission.refresh_from_db()

        assert run.are_outputs_viewable is False
        assert run.submission.is_content_viewable is False


@pytest.mark.django_db
class TestTerminalRetentionScheduling:
    """Every terminal outcome must produce a deterministic purge deadline."""

    def test_finite_deadline_starts_at_completion(self):
        """Long-running jobs deserve the full author-selected review window."""

        ended_at = timezone.now() - timedelta(hours=3)
        run = ValidationRunFactory(
            status=ValidationRunStatus.SUCCEEDED,
            ended_at=ended_at,
            output_retention_policy=OutputRetention.STORE_7_DAYS,
            output_expires_at=None,
        )

        schedule_terminal_retention(run)
        run.refresh_from_db()

        assert run.output_expires_at == ended_at + timedelta(days=7)

    def test_missing_completion_time_is_stamped_once(self, monkeypatch):
        """Duplicate finalizers must not extend an abnormal row's deadline."""

        first_finalization = timezone.now()
        run = ValidationRunFactory(
            status=ValidationRunStatus.FAILED,
            ended_at=None,
            output_retention_policy=OutputRetention.STORE_1_DAY,
            output_expires_at=None,
        )
        monkeypatch.setattr(timezone, "now", lambda: first_finalization)

        schedule_terminal_retention(run)
        run.refresh_from_db()

        monkeypatch.setattr(
            timezone,
            "now",
            lambda: first_finalization + timedelta(hours=4),
        )
        schedule_terminal_retention(run)
        run.refresh_from_db()

        assert run.ended_at == first_finalization
        assert run.output_expires_at == first_finalization + timedelta(days=1)

    def test_finite_input_deadline_starts_after_last_shared_run(self):
        """A shared input gets its full window after every consumer finishes."""
        first_end = timezone.now() - timedelta(hours=3)
        first_run = ValidationRunFactory(
            status=ValidationRunStatus.SUCCEEDED,
            ended_at=first_end,
            submission__retention_policy=SubmissionRetention.STORE_7_DAYS,
        )
        submission = first_run.submission
        sibling = ValidationRunFactory(
            submission=submission,
            status=ValidationRunStatus.RUNNING,
        )
        provisional_expiry = submission.expires_at

        schedule_terminal_retention(first_run)
        submission.refresh_from_db()
        assert submission.expires_at == provisional_expiry

        last_end = timezone.now()
        sibling.status = ValidationRunStatus.SUCCEEDED
        sibling.ended_at = last_end
        sibling.save(update_fields=["status", "ended_at"])
        schedule_terminal_retention(sibling)
        submission.refresh_from_db()

        assert submission.expires_at == last_end + timedelta(days=7)

    def test_permanent_retention_has_no_deadline(self):
        """Permanent storage must remain an explicit non-expiring choice."""

        run = ValidationRunFactory(
            status=ValidationRunStatus.SUCCEEDED,
            ended_at=timezone.now(),
            output_retention_policy=OutputRetention.STORE_PERMANENTLY,
            output_expires_at=timezone.now(),
        )

        schedule_terminal_retention(run)
        run.refresh_from_db()

        assert run.output_expires_at is None

    def test_unknown_policy_is_normalized_to_no_retention(self):
        """Legacy/corrupt rows must be made immediately purge-eligible."""

        ended_at = timezone.now() - timedelta(minutes=2)
        run = ValidationRunFactory(
            status=ValidationRunStatus.FAILED,
            ended_at=ended_at,
            output_retention_policy="UNKNOWN",
            output_expires_at=None,
        )

        schedule_terminal_retention(run)
        run.refresh_from_db()

        assert run.output_retention_policy == OutputRetention.DO_NOT_STORE
        assert run.output_expires_at == ended_at

    def test_unknown_input_policy_is_normalized_and_queued(self):
        """A corrupt input policy must fail closed through the common hook."""
        run = ValidationRunFactory(
            status=ValidationRunStatus.FAILED,
            ended_at=timezone.now(),
        )
        type(run.submission).objects.filter(pk=run.submission_id).update(
            retention_policy="UNKNOWN",
        )
        run.submission.refresh_from_db()

        schedule_terminal_retention(run)
        run.submission.refresh_from_db()

        assert run.submission.retention_policy == SubmissionRetention.DO_NOT_STORE
        assert PurgeRetry.objects.filter(submission=run.submission).exists()

    def test_nonterminal_run_is_ignored(self):
        """Retention scheduling must never make an active bundle eligible."""

        run = ValidationRunFactory(
            status=ValidationRunStatus.RUNNING,
            output_retention_policy=OutputRetention.DO_NOT_STORE,
        )

        schedule_terminal_retention(run)
        run.refresh_from_db()

        assert run.output_expires_at is None
        assert not PurgeRetry.objects.filter(submission=run.submission).exists()

    def test_finalized_signal_schedules_both_input_and_output(self):
        """One common terminal signal prevents failure/cancel path drift."""

        ended_at = timezone.now() - timedelta(minutes=2)
        run = ValidationRunFactory(
            status=ValidationRunStatus.CANCELED,
            ended_at=ended_at,
            output_retention_policy=OutputRetention.DO_NOT_STORE,
            submission__retention_policy=SubmissionRetention.DO_NOT_STORE,
        )

        responses = validation_run_finalized.send_robust(
            sender=type(self),
            validation_run=run,
        )
        run.refresh_from_db()

        assert all(not isinstance(response, Exception) for _, response in responses)
        assert run.output_expires_at == ended_at
        assert PurgeRetry.objects.filter(submission=run.submission).exists()
