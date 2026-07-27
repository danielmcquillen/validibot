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
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.models import PurgeRetry
from validibot.submissions.models import Submission
from validibot.submissions.models import SubmissionInputFile
from validibot.submissions.models import queue_submission_purge
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.models import ValidationRun
from validibot.validations.tests.factories import ValidationRunFactory

EXPIRY_ASSERTION_TOLERANCE_SECONDS = 30


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

    def test_purge_content_minimizes_payload_derived_metadata(self):
        """No-retention must clear arbitrary labels while keeping audit facts."""
        submission = SubmissionFactory(
            content='{"test": "data"}',
            name="Customer secret",
            checksum_sha256="abc123",
            original_filename="test.json",
            size_bytes=100,
            metadata={"customer": "sensitive"},
        )

        submission.purge_content()
        submission.refresh_from_db()

        assert submission.name == ""
        assert submission.checksum_sha256 == "abc123"
        assert submission.original_filename == ""
        assert submission.size_bytes == 100  # noqa: PLR2004
        assert submission.metadata == {}

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

    def test_purge_content_clears_submitted_port_files(self, tmp_path):
        """Purging should delete extra artifact-port files too.

        Multi-file EnergyPlus launches can store a submitted EPW alongside the
        primary model. Retention guarantees apply to both files; otherwise a
        DO_NOT_STORE submission would still leave launch-time auxiliary inputs
        behind after purge.
        """
        with override_settings(MEDIA_ROOT=str(tmp_path)):
            submission = SubmissionFactory(content='{"test": "data"}')
            port_file = SubmissionInputFile(
                submission=submission,
                port_key="weather_file",
            )
            port_file.set_file(
                uploaded_file=ContentFile(b"LOCATION,Test Weather"),
                filename="weather.epw",
            )
            port_file.full_clean()
            port_file.save()
            checksum = port_file.checksum_sha256

            submission.purge_content()
            port_file.refresh_from_db()

        assert not port_file.input_file
        assert port_file.file_purged_at is not None
        assert port_file.original_filename == ""
        assert port_file.content_type == ""
        assert port_file.metadata == {}
        assert port_file.checksum_sha256 == checksum

    @patch("validibot.submissions.models._delete_run_input_files")
    def test_purge_content_deletes_copied_run_inputs(self, mock_delete):
        """Input purge must remove copied inputs from each terminal run."""
        submission = SubmissionFactory(content='{"test": "data"}')
        run = ValidationRunFactory(submission=submission, status="FAILED")

        submission.purge_content()

        # Should have been called for the related run
        mock_delete.assert_called_once_with(run)

    @patch("validibot.submissions.models._delete_run_input_files")
    def test_purge_content_preserves_input_when_run_deletion_fails(
        self,
        mock_delete,
        tmp_path,
    ):
        """A failed run-bundle delete must leave the submission retryable.

        Retention truth depends on keeping both the database identity and any
        not-yet-deleted input bytes until every required run bundle is gone.
        Deleting the input first would leave an unpurged record whose content
        could no longer be read or retried coherently.
        """
        mock_delete.side_effect = OSError("object storage unavailable")

        with override_settings(MEDIA_ROOT=str(tmp_path)):
            submission = SubmissionFactory(content='{"test": "data"}')
            submission.input_file.save(
                "submission.json",
                ContentFile(b'{"test": "data"}'),
                save=False,
            )
            submission.content = ""
            submission.save(update_fields=["content", "input_file"])
            ValidationRunFactory(submission=submission, status="FAILED")
            original_file_name = submission.input_file.name

            with pytest.raises(OSError, match="object storage unavailable"):
                submission.purge_content()

            submission.refresh_from_db()
            assert submission.content_purged_at is None
            assert submission.input_file.name == original_file_name
            assert submission.input_file.storage.exists(original_file_name)

    @override_settings(GCS_VALIDATION_BUCKET="test-validation-bucket")
    @patch("validibot.validations.services.cloud_run.gcs_client.delete_prefix_except")
    def test_delete_run_inputs_targets_validation_bucket_and_preserves_outputs(
        self,
        mock_delete_prefix_except,
    ):
        """GCS input deletion must preserve independently retained outputs.

        The scan still targets the launcher's raw validation bucket rather than
        DataStorage's ``private/`` namespace, but output.json and outputs/ are
        explicit exclusions until the run's output policy expires.
        """
        mock_delete_prefix_except.return_value = 0
        submission = SubmissionFactory(content='{"test": "data"}')
        run = ValidationRunFactory(submission=submission, status="FAILED")

        submission.purge_content()

        expected_uri = f"gs://test-validation-bucket/runs/{run.org_id}/{run.id}/"
        mock_delete_prefix_except.assert_called_once()
        call = mock_delete_prefix_except.call_args
        assert call.args == (expected_uri,)
        assert f"{expected_uri}output.json" in call.kwargs["keep_uris"]
        assert f"{expected_uri}outputs/" in call.kwargs["keep_prefixes"]

    def test_delete_run_inputs_local_preserves_output_directory(self, tmp_path):
        """Local input purge must remove inputs without shortening outputs.

        This covers both legacy root-level envelopes and validator-produced
        files below outputs/.
        """
        submission = SubmissionFactory(content='{"test": "data"}')
        run = ValidationRunFactory(submission=submission, status="FAILED")

        run_dir = tmp_path / "files" / "runs" / str(run.org_id) / str(run.id)
        outputs_dir = run_dir / "outputs"
        outputs_dir.mkdir(parents=True)
        (run_dir / "input.json").write_text("{}")
        (run_dir / "customer-model.json").write_text("{}")
        (run_dir / "output.json").write_text("{}")
        (outputs_dir / "report.json").write_text("{}")

        with override_settings(GCS_VALIDATION_BUCKET="", MEDIA_ROOT=str(tmp_path)):
            submission.purge_content()

        assert not (run_dir / "input.json").exists()
        assert not (run_dir / "customer-model.json").exists()
        assert (run_dir / "output.json").exists()
        assert (outputs_dir / "report.json").exists()

    def test_purge_content_refuses_an_active_run(self):
        """No scheduler race may delete bytes still needed by a validator."""
        submission = SubmissionFactory(content='{"test": "data"}')
        ValidationRunFactory(submission=submission, status="RUNNING")

        with pytest.raises(RuntimeError, match="still required by an active run"):
            submission.purge_content()

        submission.refresh_from_db()
        assert submission.content_purged_at is None
        assert submission.content


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

    def test_alert_threshold_does_not_stop_retries(self):
        """Deletion must continue after the operator alert threshold."""
        submission = SubmissionFactory()
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now(),
            attempt_count=PurgeRetry.MAX_ATTEMPTS - 1,
        )

        retry.record_failure("Final attempt")
        retry.refresh_from_db()

        assert retry.attempt_count == PurgeRetry.MAX_ATTEMPTS
        assert retry.next_retry_at <= timezone.now() + timedelta(days=1, minutes=1)

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
            retention_policy=SubmissionRetention.STORE_7_DAYS,
        )
        # Set expires_at in the past
        Submission.objects.filter(id=expired_submission.id).update(
            expires_at=timezone.now() - timedelta(hours=1),
            created=timezone.now() - timedelta(days=8),
        )

        # Create non-expired submission
        future_submission = SubmissionFactory(
            retention_policy=SubmissionRetention.STORE_30_DAYS,
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

    def test_expired_submission_is_deferred_while_run_is_active(self):
        """A wall-clock deadline must never break an in-flight validation."""
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.STORE_7_DAYS,
        )
        ValidationRunFactory(submission=submission, status="RUNNING")
        Submission.objects.filter(id=submission.id).update(
            expires_at=timezone.now() - timedelta(hours=1),
            created=timezone.now() - timedelta(days=8),
        )

        call_command("purge_expired_submissions")

        submission.refresh_from_db()
        assert submission.content_purged_at is None

    def test_dry_run_does_not_purge(self):
        """Dry run should report but not actually purge."""
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.STORE_7_DAYS,
        )
        Submission.objects.filter(id=submission.id).update(
            expires_at=timezone.now() - timedelta(hours=1),
            created=timezone.now() - timedelta(days=8),
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
            sub = SubmissionFactory(retention_policy=SubmissionRetention.STORE_7_DAYS)
            Submission.objects.filter(id=sub.id).update(
                expires_at=timezone.now() - timedelta(hours=1),
                created=timezone.now() - timedelta(days=8),
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

    def test_missed_terminal_hook_repairs_input_window_before_purge(self):
        """Repair must not shorten a finite window after a long validation."""
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.STORE_7_DAYS,
        )
        ended_at = timezone.now()
        ValidationRunFactory(
            submission=submission,
            status="SUCCEEDED",
            ended_at=ended_at,
        )
        Submission.objects.filter(id=submission.id).update(
            created=timezone.now() - timedelta(days=8),
            expires_at=timezone.now() - timedelta(hours=1),
        )

        call_command("purge_expired_submissions")

        submission.refresh_from_db()
        assert submission.content_purged_at is None
        assert submission.expires_at == ended_at + timedelta(days=7)

    def test_skips_already_purged(self):
        """Command should skip submissions that are already purged."""
        # Create a valid submission first
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.STORE_7_DAYS,
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
        """Future retries must remain untouched until their due time arrives.

        The assertion targets the specific retry created in this test instead
        of assuming the command runs against an otherwise empty retry table.
        """
        submission = SubmissionFactory(content='{"test": "data"}')
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() + timedelta(hours=1),
        )

        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        # Submission should not be purged (retry not due yet)
        submission.refresh_from_db()
        assert submission.content_purged_at is None
        retry.refresh_from_db()
        assert retry.attempt_count == 0
        assert PurgeRetry.objects.filter(id=retry.id).exists()
        assert str(submission.id) not in out.getvalue()

    def test_processes_retries_beyond_alert_threshold(self):
        """Privacy-critical deletion must not depend on manual intervention."""
        submission = SubmissionFactory(content='{"test": "data"}')
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() - timedelta(minutes=5),
            attempt_count=PurgeRetry.MAX_ATTEMPTS,  # Exceeded
        )

        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        # The retry still runs and succeeds.
        submission.refresh_from_db()
        assert submission.content_purged_at is not None
        assert not PurgeRetry.objects.filter(id=retry.id).exists()
        assert str(submission.id) in out.getvalue()

    @patch.object(Submission, "purge_content")
    def test_reports_alert_threshold_retries_while_continuing(self, mock_purge):
        """Operators need an alert without deletion work being abandoned."""
        mock_purge.side_effect = OSError("storage still unavailable")
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

        # The failed attempt remains scheduled and is reported for investigation.
        assert "crossed the alert threshold" in out.getvalue()
        stale_retry = PurgeRetry.objects.get(submission=stale_submission)
        assert stale_retry.attempt_count > PurgeRetry.MAX_ATTEMPTS

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
        PurgeRetry.objects.all().delete()

        out = StringIO()
        call_command("process_purge_retries", stdout=out)

        assert "No pending purge retries" in out.getvalue()


@pytest.mark.django_db
class TestQueueSubmissionPurge:
    """Tests for queue_submission_purge() helper."""

    def test_creates_retry_for_do_not_store_submission(self):
        """queue_submission_purge should enqueue a purge retry for DO_NOT_STORE."""
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.DO_NOT_STORE,
        )

        queue_submission_purge(submission)

        assert PurgeRetry.objects.filter(submission=submission).exists()

    def test_noop_when_submission_already_purged(self):
        """
        queue_submission_purge should not create retries for already-purged content.
        """
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.DO_NOT_STORE
        )
        Submission.objects.filter(id=submission.id).update(
            content="",
            input_file="",
            content_purged_at=timezone.now(),
        )

        submission.refresh_from_db()
        queue_submission_purge(submission)

        assert not PurgeRetry.objects.filter(submission=submission).exists()

    def test_bring_next_retry_forward_when_scheduled_in_future(self):
        """queue_submission_purge should bring next_retry_at forward for fast purge."""
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.DO_NOT_STORE
        )
        retry = PurgeRetry.objects.create(
            submission=submission,
            next_retry_at=timezone.now() + timedelta(hours=1),
        )

        queue_submission_purge(submission)

        retry.refresh_from_db()
        assert retry.next_retry_at <= timezone.now()

    def test_active_sibling_run_blocks_submission_purge(self):
        """Shared submission bytes must survive until every run is terminal."""

        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.DO_NOT_STORE,
        )
        ValidationRunFactory(
            submission=submission,
            status="RUNNING",
        )

        queue_submission_purge(submission)

        assert not PurgeRetry.objects.filter(submission=submission).exists()

    def test_terminal_run_allows_submission_purge(self):
        """The last terminal run should make no-retention input work eligible."""

        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.DO_NOT_STORE,
        )
        ValidationRunFactory(
            submission=submission,
            status="FAILED",
            ended_at=timezone.now(),
        )

        queue_submission_purge(submission)

        assert PurgeRetry.objects.filter(submission=submission).exists()


@pytest.mark.django_db
class TestPurgeRepairDiscovery:
    """Scheduled repair must recover work missed by terminal callbacks."""

    def test_terminal_no_retention_submission_is_rediscovered(self):
        """A lost signal must not turn no-retention input into permanence."""

        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.DO_NOT_STORE,
        )
        ValidationRunFactory(
            submission=submission,
            status="FAILED",
            ended_at=timezone.now() - timedelta(minutes=5),
        )

        call_command("process_purge_retries")

        submission.refresh_from_db()
        assert submission.content_purged_at is not None


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
class TestSubmissionRetentionPolicy:
    """Tests for data retention policy constants and behavior."""

    def test_retention_policy_choices(self):
        """SubmissionRetention should have expected choices."""
        choices = dict(SubmissionRetention.choices)
        assert SubmissionRetention.DO_NOT_STORE in choices
        assert SubmissionRetention.STORE_7_DAYS in choices
        assert SubmissionRetention.STORE_30_DAYS in choices

    def test_submission_stores_retention_policy(self):
        """Submission should store the retention policy correctly."""
        submission = SubmissionFactory()

        # Default should be DO_NOT_STORE
        assert submission.retention_policy == SubmissionRetention.DO_NOT_STORE

        # Can set other policies
        submission.retention_policy = SubmissionRetention.STORE_30_DAYS
        submission.save()
        submission.refresh_from_db()
        assert submission.retention_policy == SubmissionRetention.STORE_30_DAYS

    def test_finite_retention_sets_expiry_timestamp(self):
        """Finite retention policies need an expiry for scheduled purge jobs."""
        before = timezone.now()
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.STORE_7_DAYS,
            expires_at=None,
        )
        submission.refresh_from_db()

        assert submission.expires_at is not None
        expected = before + timedelta(days=7)
        assert (
            abs((submission.expires_at - expected).total_seconds())
            < EXPIRY_ASSERTION_TOLERANCE_SECONDS
        )

    def test_permanent_retention_clears_expiry_timestamp(self):
        """Permanent storage must not leave a stale purge timestamp behind."""
        submission = SubmissionFactory(
            retention_policy=SubmissionRetention.STORE_7_DAYS,
        )

        submission.retention_policy = SubmissionRetention.STORE_PERMANENTLY
        submission.save()
        submission.refresh_from_db()

        assert submission.expires_at is None

    def test_public_workflow_launcher_can_own_submission(self):
        """Public launches should not fail model validation for non-members.

        ``WorkflowVisibility.ALL_USERS`` is the new home of the old
        ``is_public=True``: any authenticated user can launch, so a
        non-member's submission must still pass model validation.
        """
        from validibot.users.tests.factories import UserFactory
        from validibot.workflows.constants import WorkflowVisibility
        from validibot.workflows.tests.factories import WorkflowFactory

        workflow = WorkflowFactory(
            workflow_visibility=WorkflowVisibility.ALL_USERS,
        )
        user = UserFactory()
        submission = SubmissionFactory(
            org=workflow.org,
            workflow=workflow,
            project=workflow.project,
            user=user,
        )

        submission.full_clean()
