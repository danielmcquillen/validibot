from __future__ import annotations

from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from validibot.projects.tests.factories import ProjectFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import ValidationType
from validibot.validations.services.fmu import create_fmu_validator
from validibot.validations.services.fmu import run_fmu_probe


def _make_fake_fmu(name: str = "demo") -> SimpleUploadedFile:
    """Load the canned Feedthrough FMU from test assets."""

    asset = (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "assets"
        / "fmu"
        / "Feedthrough.fmu"
    )
    payload = asset.read_bytes()
    return SimpleUploadedFile(
        asset.name, payload, content_type="application/octet-stream"
    )


class FMUServiceTests(TestCase):
    """Exercises FMU creation and probe flows for FMU validators."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.project = ProjectFactory(org=self.org)

    def test_create_fmu_validator_introspects_and_seeds_catalog(self):
        upload = _make_fake_fmu()

        validator = create_fmu_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )

        self.assertEqual(validator.validation_type, ValidationType.FMU)
        # Feedthrough FMU declares 4 inputs and 4 outputs in modelDescription.xml.
        self.assertEqual(
            validator.catalog_entries.filter(run_stage=CatalogRunStage.INPUT).count(),
            4,
        )
        self.assertEqual(
            validator.catalog_entries.filter(run_stage=CatalogRunStage.OUTPUT).count(),
            4,
        )
        fmu_model = validator.fmu_model
        self.assertIsNotNone(fmu_model)
        # The Feedthrough FMU defines 11 variables (inputs, outputs, parameters).
        self.assertEqual(fmu_model.variables.count(), 11)
        self.assertTrue(fmu_model.is_approved)
        self.assertTrue(fmu_model.file.name)
        self.assertTrue(fmu_model.file.storage.exists(fmu_model.file.name))
        self.assertEqual(fmu_model.gcs_uri, "")

    def test_run_fmu_probe_refreshes_variables(self):
        """Probe parses modelDescription.xml in-process and refreshes catalog."""
        upload = _make_fake_fmu()
        validator = create_fmu_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )
        fmu_model = validator.fmu_model
        self.assertIsNotNone(fmu_model)

        result = run_fmu_probe(fmu_model)

        # Probe should succeed since parsing happens in-process
        self.assertEqual(result.status, "success")
        # Feedthrough FMU declares 11 variables
        self.assertEqual(len(result.variables), 11)
        self.assertIsNotNone(result.execution_seconds)
        self.assertGreater(result.execution_seconds, 0)

        # Verify FMU model was updated
        fmu_model.refresh_from_db()
        self.assertTrue(fmu_model.is_approved)

        # Catalog entries should still match (4 inputs + 4 outputs)
        self.assertEqual(
            validator.catalog_entries.filter(run_stage=CatalogRunStage.INPUT).count(),
            4,
        )
        self.assertEqual(
            validator.catalog_entries.filter(run_stage=CatalogRunStage.OUTPUT).count(),
            4,
        )
