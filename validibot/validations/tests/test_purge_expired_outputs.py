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
from django.core.management import call_command
from django.utils import timezone

from validibot.submissions.constants import OutputRetention
from validibot.validations.models import Artifact
from validibot.validations.models import ValidationFinding
from validibot.validations.tests.factories import ArtifactFactory
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
