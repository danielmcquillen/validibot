from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.validations.constants import CatalogRunStage, ValidationType
from simplevalidations.validations.services.fmi import create_fmi_validator, run_fmu_probe
from sv_shared.fmi import FMIProbeResult, FMIVariableMeta


def _make_fake_fmu(name: str = "demo") -> SimpleUploadedFile:
    """Load the canned Feedthrough FMU from test assets."""

    from pathlib import Path

    asset = Path(__file__).resolve().parents[3] / "tests" / "assets" / "fmu" / "Feedthrough.fmu"
    payload = asset.read_bytes()
    return SimpleUploadedFile(asset.name, payload, content_type="application/octet-stream")


class FMIServiceTests(TestCase):
    """Exercises FMU creation and probe flows for FMI validators."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.project = ProjectFactory(org=self.org)

    def tearDown(self):
        from simplevalidations.validations.services import fmi as fmi_module

        fmi_module._FMIProbeRunner.configure_modal_runner(None)  # type: ignore[attr-defined]

    def test_create_fmi_validator_introspects_and_seeds_catalog(self):
        upload = _make_fake_fmu()

        validator = create_fmi_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )

        self.assertEqual(validator.validation_type, ValidationType.FMI)
        self.assertEqual(
            validator.catalog_entries.filter(run_stage=CatalogRunStage.INPUT).count(),
            1,
        )
        self.assertEqual(
            validator.catalog_entries.filter(run_stage=CatalogRunStage.OUTPUT).count(),
            1,
        )
        fmu_model = validator.fmu_model
        self.assertIsNotNone(fmu_model)
        self.assertEqual(fmu_model.variables.count(), 2)
        self.assertTrue(fmu_model.is_approved)

    def test_run_fmu_probe_refreshes_variables(self):
        upload = _make_fake_fmu()
        validator = create_fmi_validator(
            org=self.org,
            project=self.project,
            name="Test FMU",
            upload=upload,
        )
        fmu_model = validator.fmu_model
        self.assertIsNotNone(fmu_model)

        probe_result = FMIProbeResult.success(
            variables=[
                FMIVariableMeta(
                    name="a",
                    causality="input",
                    value_type="Real",
                    value_reference=1,
                ),
            ],
        )

        class FakeProbeRunner:
            """Capture Modal probe invocations and return a canned response."""

            def __init__(self, response: dict):
                self.response = response
                self.calls: list[dict] = []

            def __call__(self, **kwargs):
                self.calls.append(kwargs)
                return self.response

        fake_runner = FakeProbeRunner(probe_result.model_dump(mode="json"))
        from simplevalidations.validations.services import fmi as fmi_module

        fmi_module._FMIProbeRunner.configure_modal_runner(fake_runner)  # type: ignore[attr-defined]

        run_fmu_probe(fmu_model)

        self.assertEqual(fmu_model.variables.count(), 1)
