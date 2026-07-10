"""Regression tests for validation artifact identity and storage paths.

Artifacts belong to both an organization and a validation run. Their model
validation must enforce that relationship, while their storage path must use
the same run identity so files remain isolated and discoverable. These tests
guard against stale field names making either operation crash at runtime.
"""

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.models import Artifact
from validibot.validations.tests.factories import ValidationRunFactory


@pytest.mark.django_db
class TestArtifactModel:
    """Keep artifact organization, run, and file identities aligned."""

    def test_file_save_scopes_path_to_organization_and_validation_run(self):
        """Saving an artifact must not fail or lose its owning run identity.

        The path is the storage boundary used to find and purge a run's files.
        Using a stale foreign-key name here prevents every real artifact file
        from being saved and would also break retention by scattering files
        outside the run-scoped prefix.
        """
        run = ValidationRunFactory()
        artifact = Artifact(
            org=run.org,
            validation_run=run,
            label="Validation report",
        )

        artifact.file.save(
            "report.txt",
            ContentFile(b"validation output"),
            save=False,
        )

        expected_prefix = f"artifacts/org-{run.org_id}/runs/{run.id}/"
        assert artifact.file.name.startswith(expected_prefix)
        assert artifact.file.name.endswith("/report.txt")
        assert artifact.file.storage.exists(artifact.file.name)

    def test_clean_rejects_an_organization_outside_the_validation_run(self):
        """Model validation must reject cross-organization artifact links.

        This invariant protects tenant isolation even when an artifact is
        created outside a form. The check must inspect ``validation_run``;
        referring to a removed ``run`` field turns the intended validation
        boundary into an ``AttributeError`` instead.
        """
        run = ValidationRunFactory()
        artifact = Artifact(
            org=OrganizationFactory(),
            validation_run=run,
            label="Validation report",
            file="artifacts/existing-report.txt",
        )

        with pytest.raises(ValidationError) as exc_info:
            artifact.full_clean()

        assert "org" in exc_info.value.message_dict
