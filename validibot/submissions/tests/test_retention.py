"""
Tests for ephemeral retention functionality.

This module tests:
- Submission.purge_content() method
- PurgeRetry model and exponential backoff
- purge_expired_submissions management command
- process_purge_retries management command
- Nullable submission on ValidationRun (SET_NULL behavior)
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from validibot.submissions.constants import DataRetention
from validibot.submissions.models import PurgeRetry
from validibot.submissions.models import Submission
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.models import ValidationRun
from validibot.validations.tests.factories import ValidationRunFactory


@pytest.mark.django_db
class TestSubmissionPurgeContent:
    """Tests for Submission.purge_content() method."""

    def test_purge_content_clears_inline_content(self):
        """Purging should clear inline content and set purged timestamp."""
        submission = SubmissionFactory(content='{"test": "data"}')

        assert submission.content == '{"test": "data"}'
        assert submission.content_purged_at is None

        submission.purge_content()
        submission.refresh_from_db()

        assert submission.content == ""
        assert submission.content_purged_at is not None
        assert submission.expires_at is None

    def test_purge_content_preserves_metadata(self):
        """Purging should preserve audit metadata like checksum and size."""
        submission = SubmissionFactory(
            content='{"test": "data"}',
            checksum_sha256="abc123",
            original_filename="test.json",
            size_bytes=100,
        )

        submission.purge_content()
        submission.refresh_from_db()

        # Metadata preserved for audit trail
        assert submission.checksum_sha256 == "abc123"
        assert submission.original_filename == "test.json"
        assert submission.size_bytes == 100  # noqa: PLR2004

    def test_purge_content_is_idempotent(self):
        """Calling purge_content() multiple times should be safe."""
        submission = SubmissionFactory(content='{"test": "data"}')

        submission.purge_content()
        first_purge_time = submission.content_purged_at

        # Call again - should be no-op
        submission.purge_content()
        submission.refresh_from_db()

        # Timestamp unchanged (idempotent)
        assert submission.content_purged_at == first_purge_time

    def test_get_content_returns_empty_after_purge(self):
        """get_content() should return empty string after purge."""
        submission = SubmissionFactory(content='{"test": "data"}')

        assert submission.get_content() == '{"test": "data"}'

        submission.purge_content()

        assert submission.get_content() == ""

    def test_is_content_available_false_after_purge(self):
        """is_content_available should return False after purge."""
        submission = SubmissionFactory(content='{"test": "data"}')

        assert submission.is_content_available is True

        submission.purge_content()

        assert submission.is_content_available is False

    @patch("validibot.submissions.models._delete_execution_bundle")
    def test_purge_content_deletes_execution_bundles(self, mock_delete):
        """Purging should attempt to delete GCS execution bundles."""
        submission = SubmissionFactory(content='{"test": "data"}')
        run = ValidationRunFactory(submission=submission)

        submission.purge_content()

        # Should have been called for the related run
        mock_delete.assert_called_once_with(run)


@pytest.mark.django_db
class TestPurgeRetryModel:
    """Tests for PurgeRetry model and exponential backoff."""

    def test_record_failure_increments_attempt_count(self):
        """record_failure() should increment attempt_count."""
        submission = SubmissionFactory()
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now(),
        )

        assert retry.attempt_count == 0

        retry.record_failure("Test error")

        assert retry.attempt_count == 1
        assert retry.last_error == "Test error"

    def test_record_failure_uses_exponential_backoff(self):
        """record_failure() should schedule next retry with increasing delays."""
        submission = SubmissionFactory()
        now = timezone.now()

        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=now,
        )

        # First failure: 60 seconds
        retry.record_failure("Error 1")
        retry.refresh_from_db()
        assert retry.attempt_count == 1
        # Allow some tolerance for test execution time
        expected_delay_1 = timedelta(seconds=60)
        actual_delay_1 = retry.next_retry_at - retry.last_attempt_at
        assert actual_delay_1 >= expected_delay_1 - timedelta(seconds=5)
        assert actual_delay_1 <= expected_delay_1 + timedelta(seconds=5)

        # Second failure: 300 seconds (5 minutes)
        retry.record_failure("Error 2")
        retry.refresh_from_db()
        assert retry.attempt_count == 2  # noqa: PLR2004
        expected_delay_2 = timedelta(seconds=300)
        actual_delay_2 = retry.next_retry_at - retry.last_attempt_at
        assert actual_delay_2 >= expected_delay_2 - timedelta(seconds=5)
        assert actual_delay_2 <= expected_delay_2 + timedelta(seconds=5)

    def test_max_attempts_stops_retries(self):
        """After MAX_ATTEMPTS, next_retry should be set to far future."""
        submission = SubmissionFactory()
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now(),
            attempt_count=PurgeRetry.MAX_ATTEMPTS - 1,
        )

        retry.record_failure("Final attempt")
        retry.refresh_from_db()

        assert retry.attempt_count == PurgeRetry.MAX_ATTEMPTS
        # Next retry should be ~1 year in future (effectively stopped)
        assert retry.next_retry_at > timezone.now() + timedelta(days=300)

    def test_record_failure_truncates_long_errors(self):
        """Long error messages should be truncated to prevent DB issues."""
        submission = SubmissionFactory()
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now(),
        )

        long_error = "x" * 3000
        retry.record_failure(long_error)
        retry.refresh_from_db()

        assert len(retry.last_error) == 2000  # noqa: PLR2004


@pytest.mark.django_db
class TestPurgeExpiredSubmissionsCommand:
    """Tests for purge_expired_submissions management command."""

    def test_purges_expired_submissions(self):
        """Command should purge submissions past their expires_at date."""
        # Create expired submission (not DO_NOT_STORE)
        expired_submission = SubmissionFactory(
            retention_policy=DataRetention.STORE_10_DAYS,
        )
        # Set expires_at in the past
        Submission.objects.filter(id=expired_submission.id).update(
            expires_at=timezone.now() - timedelta(hours=1),
        )

        # Create non-expired submission
        future_submission = SubmissionFactory(
            retention_policy=DataRetention.STORE_30_DAYS,
        )
        Submission.objects.filter(id=future_submission.id).update(
            expires_at=timezone.now() + timedelta(days=30),
        )

        out = StringIO()
        call_command("purge_expired_submissions", stdout=out)

        # Expired submission should be purged
        expired_submission.refresh_from_db()
        assert expired_submission.content_purged_at is not None

        # Non-expired submission should be untouched
        future_submission.refresh_from_db()
        assert future_submission.content_purged_at is None

    def test_dry_run_does_not_purge(self):
        """Dry run should report but not actually purge."""
        submission = SubmissionFactory(
            retention_policy=DataRetention.STORE_10_DAYS,
        )
        Submission.objects.filter(id=submission.id).update(
            expires_at=timezone.now() - timedelta(hours=1),
        )

        out = StringIO()
        call_command("purge_expired_submissions", "--dry-run", stdout=out)

        # Should not be purged
        submission.refresh_from_db()
        assert submission.content_purged_at is None
        assert "DRY RUN" in out.getvalue()

    def test_respects_max_batches(self):
        """Command should respect --max-batches parameter."""
        # Create 5 expired submissions
        for _ in range(5):
            sub = SubmissionFactory(retention_policy=DataRetention.STORE_10_DAYS)
            Submission.objects.filter(id=sub.id).update(
                expires_at=timezone.now() - timedelta(hours=1),
            )

        out = StringIO()
        # Process only 1 batch of 2
        call_command(
            "purge_expired_submissions",
            "--batch-size=2",
            "--max-batches=1",
            stdout=out,
        )

        # Should have purged exactly 2
        purged_count = Submission.objects.filter(
            content_purged_at__isnull=False,
        ).count()
        assert purged_count == 2  # noqa: PLR2004
        assert "Reached max batch limit" in out.getvalue()

    def test_no_expired_submissions(self):
        """Command should report when no expired submissions exist."""
        out = StringIO()
        call_command("purge_expired_submissions", stdout=out)

        assert "No expired submissions to purge" in out.getvalue()

    def test_skips_already_purged(self):
        """Command should skip submissions that are already purged."""
        # Create a valid submission first
        submission = SubmissionFactory(
            retention_policy=DataRetention.STORE_10_DAYS,
            content='{"test": "data"}',
        )
        # Mark as already purged using .update() to bypass model validation
        Submission.objects.filter(id=submission.id).update(
            content="",
            input_file="",
            expires_at=timezone.now() - timedelta(hours=1),
            content_purged_at=timezone.now() - timedelta(hours=2),
        )

        out = StringIO()
        call_command("purge_expired_submissions", stdout=out)

        # Should not try to purge already-purged submissions
        assert "No expired submissions to purge" in out.getvalue()


@pytest.mark.django_db
class TestProcessPurgeRetriesCommand:
    """Tests for process_purge_retries management command."""

    def test_processes_pending_retries(self):
        """Command should process retries that are due."""
        submission = SubmissionFactory(content='{"test": "data"}')
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() - timedelta(minutes=5),
        )

        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        # Submission should be purged
        submission.refresh_from_db()
        assert submission.content_purged_at is not None

        # Retry record should be deleted on success
        assert not PurgeRetry.objects.filter(id=retry.id).exists()
        assert "Purged:" in out.getvalue()

    def test_skips_already_purged_submissions(self):
        """Command should skip and delete retries for already-purged submissions."""
        # Create a valid submission first
        submission = SubmissionFactory(content='{"test": "data"}')
        # Mark as already purged using .update() to bypass model validation
        Submission.objects.filter(id=submission.id).update(
            content="",
            input_file="",
            content_purged_at=timezone.now(),
        )

        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() - timedelta(minutes=5),
        )

        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        # Retry should be deleted (cleaned up)
        assert not PurgeRetry.objects.filter(id=retry.id).exists()
        assert "Skipped (already purged)" in out.getvalue()

    def test_dry_run_does_not_process(self):
        """Dry run should report but not process retries."""
        submission = SubmissionFactory(content='{"test": "data"}')
        PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() - timedelta(minutes=5),
        )

        out = StringIO()
        call_command("process_purge_retries", "--dry-run", stdout=out)

        # Submission should not be purged
        submission.refresh_from_db()
        assert submission.content_purged_at is None
        assert "DRY RUN" in out.getvalue()

    def test_does_not_process_future_retries(self):
        """Command should not process retries scheduled for the future."""
        submission = SubmissionFactory(content='{"test": "data"}')
        PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() + timedelta(hours=1),
        )

        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        # Submission should not be purged (retry not due yet)
        submission.refresh_from_db()
        assert submission.content_purged_at is None
        assert "No pending purge retries" in out.getvalue()

    def test_does_not_process_max_attempts_exceeded(self):
        """Command should not process retries that exceeded max attempts."""
        submission = SubmissionFactory(content='{"test": "data"}')
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() - timedelta(minutes=5),
            attempt_count=PurgeRetry.MAX_ATTEMPTS,  # Exceeded
        )

        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        # Stale retry should NOT be processed
        submission.refresh_from_db()
        assert submission.content_purged_at is None  # Not purged

        # Retry record should still exist (not deleted)
        assert PurgeRetry.objects.filter(id=retry.id).exists()

        # No pending retries message shown (stale ones excluded)
        assert "No pending purge retries" in out.getvalue()

    def test_reports_stale_retries_after_processing(self):
        """Command should report stale retries after processing pending ones."""
        # Create a processable retry
        good_submission = SubmissionFactory(content='{"processable": "data"}')
        PurgeRetry.objects.create(
            submission=good_submission,
            next_retry_at=timezone.now() - timedelta(minutes=5),
        )

        # Create a stale retry (exceeded max attempts)
        stale_submission = SubmissionFactory(content='{"stale": "data"}')
        PurgeRetry.objects.create(
            submission=stale_submission,
            next_retry_at=timezone.now() - timedelta(minutes=5),
            attempt_count=PurgeRetry.MAX_ATTEMPTS,
        )

        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        # Should report the stale retry after processing
        assert "require manual intervention" in out.getvalue()

    @patch.object(Submission, "purge_content")
    def test_records_failure_on_exception(self, mock_purge):
        """Command should record failure when purge raises exception."""
        mock_purge.side_effect = Exception("GCS unavailable")

        submission = SubmissionFactory(content='{"test": "data"}')
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() - timedelta(minutes=5),
        )

        out = StringIO()
        err = StringIO()
        call_command("process_purge_retries", stdout=out, stderr=err)

        # Retry should still exist with incremented count
        retry.refresh_from_db()
        assert retry.attempt_count == 1
        assert "GCS unavailable" in retry.last_error
        assert "Failed:" in out.getvalue()

    def test_no_pending_retries(self):
        """Command should report when no pending retries exist."""
        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        assert "No pending purge retries" in out.getvalue()


@pytest.mark.django_db
class TestValidationRunNullableSubmission:
    """Tests for ValidationRun with nullable submission (SET_NULL behavior)."""

    def test_run_survives_submission_deletion(self):
        """ValidationRun should survive when its submission is deleted."""
        submission = SubmissionFactory()
        run = ValidationRunFactory(submission=submission)
        run_id = run.id

        # Delete the submission
        submission.delete()

        # Run should still exist with null submission
        run = ValidationRun.objects.get(id=run_id)
        assert run.submission is None

    def test_run_submission_can_be_none(self):
        """ValidationRun.submission can be None without errors."""
        submission = SubmissionFactory()
        run = ValidationRunFactory(submission=submission)

        # Manually set to None (simulating SET_NULL)
        run.submission = None
        run.save()

        run.refresh_from_db()
        assert run.submission is None

    def test_accessing_none_submission_gracefully(self):
        """Code accessing run.submission should handle None gracefully."""
        submission = SubmissionFactory()
        run = ValidationRunFactory(submission=submission)
        run.submission = None
        run.save()

        # These should not raise AttributeError
        run.refresh_from_db()

        # Common patterns that should work
        name = run.submission.name if run.submission else None
        assert name is None

        content = run.submission.get_content() if run.submission else ""
        assert content == ""


@pytest.mark.django_db
class TestDataRetentionPolicy:
    """Tests for data retention policy constants and behavior."""

    def test_retention_policy_choices(self):
        """DataRetention should have expected choices."""
        choices = dict(DataRetention.choices)
        assert DataRetention.DO_NOT_STORE in choices
        assert DataRetention.STORE_10_DAYS in choices
        assert DataRetention.STORE_30_DAYS in choices

    def test_submission_stores_retention_policy(self):
        """Submission should store the retention policy correctly."""
        submission = SubmissionFactory()

        # Default should be DO_NOT_STORE
        assert submission.retention_policy == DataRetention.DO_NOT_STORE

        # Can set other policies
        submission.retention_policy = DataRetention.STORE_30_DAYS
        submission.save()
        submission.refresh_from_db()
        assert submission.retention_policy == DataRetention.STORE_30_DAYS
