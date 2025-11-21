from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.validations.services.fmi import create_fmi_validator
from sv_shared.fmi import FMIRunResult, FMIRunStatus


class FMIWorkflowIntegrationTest(TestCase):
    """
    End-to-end FMI run against Modal using the Feedthrough FMU and test volume.

    This verifies we can upload an FMU, cache it in the Modal test volume, and
    execute it remotely to echo Int32_input -> Int32_output.
    """

    def setUp(self) -> None:
        super().setUp()
        # Keep Modal traffic isolated from production by using the test volume.
        os.environ.setdefault("FMI_USE_TEST_VOLUME", "1")
        os.environ.setdefault("FMI_TEST_VOLUME_NAME", "fmi-cache-test")
        self._ensure_modal_env()
        if not os.getenv("MODAL_TOKEN_ID") or not os.getenv("MODAL_TOKEN_SECRET"):
            self.skipTest("Modal credentials not configured; skipping FMI integration test.")
        try:
            import modal  # noqa: F401
        except ImportError as err:  # pragma: no cover - defensive
            self.skipTest(f"Modal package not installed: {err}")

    def _ensure_modal_env(self) -> None:
        """
        Pull Modal credentials from Django settings into the environment so the
        Modal client can authenticate without manual sourcing.
        """

        token_id = getattr(settings, "MODAL_TOKEN_ID", "") or ""
        token_secret = getattr(settings, "MODAL_TOKEN_SECRET", "") or ""
        if token_id and token_secret:
            os.environ.setdefault("MODAL_TOKEN_ID", token_id)
            os.environ.setdefault("MODAL_TOKEN_SECRET", token_secret)

    def test_feedthrough_fmu_runs_via_modal_test_volume(self) -> None:
        """Upload Feedthrough.fmu, cache it in Modal, and assert output echoes input."""

        import modal
        import modal.exception as exc

        org = OrganizationFactory()
        project = ProjectFactory(org=org)

        asset = Path(__file__).resolve().parents[1] / "assets" / "fmu" / "linux64" / "Feedthrough.fmu"
        payload = asset.read_bytes()
        upload = SimpleUploadedFile(asset.name, payload, content_type="application/octet-stream")

        validator = create_fmi_validator(
            org=org,
            project=project,
            name="Feedthrough Integration",
            upload=upload,
        )
        fmu_model = validator.fmu_model
        self.assertIsNotNone(fmu_model)
        self.assertTrue(Path(fmu_model.file.path).exists())
        self.assertIn("/fmus-test/", fmu_model.modal_volume_path)

        try:
            runner = modal.Function.from_name("fmi-runner", "run_fmi_simulation")
        except exc.NotFoundError as err:
            self.fail(f"Modal fmi-runner.run_fmi_simulation not found: {err}")

        raw_result = runner.call(
            fmu_storage_key=fmu_model.file.path,
            fmu_url=None,
            fmu_checksum=fmu_model.checksum,
            use_test_volume=True,
            inputs={"Int32_input": 5},
            simulation_config={"start_time": 0.0, "stop_time": 1.0, "step_size": 0.1},
            output_variables=["Int32_output"],
            return_logs=False,
        )

        result = FMIRunResult.model_validate(raw_result)
        self.assertEqual(result.status, FMIRunStatus.SUCCESS)
        self.assertAlmostEqual(5.0, float(result.outputs.get("Int32_output", 0)))
