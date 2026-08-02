"""Regression tests for truthful validation-output retention.

Output purge spans PostgreSQL and external storage without a shared
transaction. These tests pin the critical rule that a storage failure must
leave database identities and purge timestamps available for a later retry;
otherwise Validibot could claim deletion while bytes remain, or discard the
only record that tells operators what still needs deleting.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from validibot.submissions.constants import OutputRetention
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import Artifact
from validibot.validations.models import RunEvidenceArtifact
from validibot.validations.models import RunEvidenceArtifactAvailability
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRunSummary
from validibot.validations.tests.factories import ArtifactFactory
from validibot.validations.tests.factories import CallbackReceiptFactory
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationFindingFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory


@pytest.mark.django_db
class TestPurgeExpiredOutputFailures:
    """Prove failed external deletion cannot become a successful purge."""

    def test_artifact_delete_failure_preserves_retry_identity(self):
        """An undeleted artifact must retain its row and unpurged run state.

        Findings are deleted before artifacts in the purge transaction, so
        retaining the finding also proves the database work rolled back rather
        than partially committing around the storage failure.
        """
        run = ValidationRunFactory(
            status=ValidationRunStatus.SUCCEEDED,
            ended_at=timezone.now() - timedelta(minutes=2),
            output_retention_policy=OutputRetention.STORE_7_DAYS,
            output_expires_at=timezone.now() - timedelta(minutes=1),
        )
        step_run = ValidationStepRunFactory(validation_run=run)
        finding = ValidationFindingFactory(
            validation_run=run,
            validation_step_run=step_run,
        )

        artifact_file_name = "artifacts/test/report.txt"
        artifact = ArtifactFactory(
            validation_run=run,
            org=run.org,
            file=artifact_file_name,
        )

        stdout = StringIO()
        stderr = StringIO()
        with patch(
            "django.db.models.fields.files.FieldFile.delete",
            side_effect=OSError("artifact storage unavailable"),
        ):
            call_command(
                "purge_expired_outputs",
                "--batch-size=10",
                "--max-batches=1",
                stdout=stdout,
                stderr=stderr,
            )

        run.refresh_from_db()
        assert run.output_purged_at is None
        assert run.output_expires_at is not None
        assert Artifact.objects.filter(pk=artifact.pk).exists()
        assert ValidationFinding.objects.filter(pk=finding.pk).exists()
        artifact.refresh_from_db()
        assert artifact.file.name == artifact_file_name
        assert "Failed to purge outputs" in stdout.getvalue()
        assert "1 run(s) failed to purge" in stderr.getvalue()


@pytest.mark.django_db
class TestCompleteOutputPurge:
    """Prove retention covers every detailed output and storage identity."""

    @override_settings(GCS_VALIDATION_BUCKET="")
    @patch("validibot.submissions.models._delete_run_files")
    def test_purge_redacts_all_payload_bearing_output_state(
        self,
        delete_run_files,
        tmp_path,
    ):
        """A purge must leave only status, counts, and operational identity."""

        with override_settings(MEDIA_ROOT=str(tmp_path)):
            run = ValidationRunFactory(
                status=ValidationRunStatus.SUCCEEDED,
                ended_at=timezone.now() - timedelta(minutes=2),
                error="validator echoed a secret",
                output_hash="f" * 64,
                output_retention_policy=OutputRetention.DO_NOT_STORE,
                output_expires_at=None,
            )
            step_run = ValidationStepRunFactory(
                validation_run=run,
                status=StepStatus.PASSED,
                output={"raw": "secret"},
                input_values={"customer": "secret"},
                output_values={"score": 42},
                error="step secret",
            )
            finding = ValidationFindingFactory(
                validation_run=run,
                validation_step_run=step_run,
                message="customer secret",
                meta={"sample": "secret"},
            )
            artifact = ArtifactFactory(
                validation_run=run,
                org=run.org,
                step_run=step_run,
                workflow_step=step_run.workflow_step,
            )
            artifact.file.save(
                "report.txt",
                ContentFile(b"sensitive output"),
                save=True,
            )
            attempt = ExecutionAttemptFactory(
                step_run=step_run,
                state=ExecutionAttemptState.COMPLETED,
                execution_bundle_uri="gs://bucket/runs/bundle",
                input_envelope_uri="gs://bucket/runs/input.json",
                output_envelope_uri="gs://bucket/runs/output.json",
                output_envelope_sha256="e" * 64,
                last_error="provider secret",
            )
            receipt = CallbackReceiptFactory(
                validation_run=run,
                execution_attempt=attempt,
                result_uri="gs://bucket/runs/output.json",
            )
            summary = ValidationRunSummary.objects.create(
                run=run,
                status=run.status,
                completed_at=run.ended_at,
                total_findings=1,
                error_count=1,
                extras={"exemplar": "customer secret"},
            )
            manifest_bytes = b'{"receipt": "permanent"}'
            evidence = RunEvidenceArtifact(
                run=run,
                schema_version=(
                    "https://validibot.com/schemas/evidence-manifest-v2.json"
                ),
                manifest_hash="a" * 64,
            )
            evidence.manifest_path.save(
                "manifest.json",
                ContentFile(manifest_bytes),
                save=True,
            )

            call_command("purge_expired_outputs")

        run.refresh_from_db()
        step_run.refresh_from_db()
        attempt.refresh_from_db()
        receipt.refresh_from_db()
        summary.refresh_from_db()
        evidence.refresh_from_db()

        assert run.output_purged_at is not None
        assert run.output_expires_at is None
        assert run.error == ""
        assert run.output_hash == ""
        assert step_run.output == {}
        assert step_run.input_values == {}
        assert step_run.output_values == {}
        assert step_run.error == ""
        assert not ValidationFinding.objects.filter(pk=finding.pk).exists()
        assert not Artifact.objects.filter(pk=artifact.pk).exists()
        assert attempt.execution_bundle_uri == ""
        assert attempt.input_envelope_uri == ""
        assert attempt.output_envelope_uri == ""
        assert attempt.output_envelope_sha256 == ""
        assert attempt.last_error == ""
        assert receipt.result_uri == ""
        assert summary.extras == {}
        assert summary.total_findings == 1
        assert evidence.availability == RunEvidenceArtifactAvailability.GENERATED
        assert evidence.manifest_path
        assert evidence.manifest_hash == "a" * 64
        with evidence.manifest_path.open("rb") as manifest_file:
            assert manifest_file.read() == manifest_bytes
        delete_run_files.assert_called_once()

    @patch(
        "validibot.validations.management.commands.purge_expired_outputs."
        "purge_run_outputs"
    )
    def test_active_run_is_never_purged_even_with_past_deadline(self, purge):
        """A stale launch-time timestamp must not delete a running bundle."""

        ValidationRunFactory(
            status=ValidationRunStatus.RUNNING,
            output_retention_policy=OutputRetention.STORE_7_DAYS,
            output_expires_at=timezone.now() - timedelta(days=1),
        )

        call_command("purge_expired_outputs")

        purge.assert_not_called()

    @patch("validibot.submissions.models._delete_run_files")
    def test_do_not_store_terminal_run_is_repaired_without_signal(
        self,
        delete_run_files,
    ):
        """The sweep must rediscover no-retention work after a missed hook."""

        run = ValidationRunFactory(
            status=ValidationRunStatus.FAILED,
            ended_at=timezone.now() - timedelta(minutes=2),
            output_retention_policy=OutputRetention.DO_NOT_STORE,
            output_expires_at=None,
        )

        call_command("purge_expired_outputs")

        run.refresh_from_db()
        assert run.output_purged_at is not None
        delete_run_files.assert_called_once_with(run)

    def test_finite_deadline_is_repaired_from_completion_time(self):
        """A missed terminal hook must not turn finite retention permanent."""

        ended_at = timezone.now() - timedelta(days=8)
        run = ValidationRunFactory(
            status=ValidationRunStatus.SUCCEEDED,
            ended_at=ended_at,
            output_retention_policy=OutputRetention.STORE_7_DAYS,
            output_expires_at=None,
        )

        with patch("validibot.submissions.models._delete_run_files"):
            call_command("purge_expired_outputs")

        run.refresh_from_db()
        assert run.output_purged_at is not None
