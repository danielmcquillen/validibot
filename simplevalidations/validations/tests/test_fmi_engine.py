from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from sv_shared.fmi import FMIRunResult

from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.engines.fmi import FMIValidationEngine
from simplevalidations.validations.models import Ruleset
from simplevalidations.validations.services.fmi import create_fmi_validator
from simplevalidations.workflows.tests.factories import WorkflowFactory


def _fake_fmu() -> SimpleUploadedFile:
    """Load the canned Feedthrough FMU from test assets."""

    from pathlib import Path

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


def _prime_modal_cache_fake():
    """
    Inject a fake cache uploader so FMI validator creation avoids real Modal calls.
    """

    from simplevalidations.validations.services import fmi as fmi_module

    calls: list[dict] = []

    def _fake_upload(**kwargs):
        calls.append(kwargs)
        return "/fmus/test-cache.fmu"

    fmi_module._upload_to_modal_volume = _fake_upload  # type: ignore[assignment] # noqa: SLF001
    return calls


class FMIEngineTests(TestCase):
    """Validate FMI engine Modal dispatch and required configuration."""

    def setUp(self):
        from simplevalidations.validations.services import fmi as fmi_module

        self._original_uploader = getattr(fmi_module, "_upload_to_modal_volume", None)

    def tearDown(self):
        FMIValidationEngine.configure_modal_runner(None)
        from simplevalidations.validations.services import fmi as fmi_module

        if self._original_uploader is not None:
            fmi_module._upload_to_modal_volume = self._original_uploader  # type: ignore[assignment] # noqa: SLF001

    def test_fmi_engine_success_path(self):
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            allowed_file_types=[SubmissionFileType.BINARY],
        )
        upload = _fake_fmu()
        _prime_modal_cache_fake()
        validator = create_fmi_validator(
            org=org,
            project=workflow.project,
            name="Test FMU",
            upload=upload,
        )
        ruleset = Ruleset.objects.create(
            org=org,
            user=None,
            name="FMI Rules",
            ruleset_type=RulesetType.FMI,
            version="1",
            rules_text="{}",
        )
        submission = SubmissionFactory(
            org=org,
            project=workflow.project,
            workflow=workflow,
            file_type=SubmissionFileType.BINARY,
        )

        class _FakeRunner:
            """Capture Modal calls and return canned FMI run outputs."""

            def __init__(self, response: dict):
                self.response = response
                self.calls: list[dict] = []

            def __call__(self, **kwargs):
                self.calls.append(kwargs)
                return self.response

        fake_result = FMIRunResult.success(outputs={"y_out": 3.0}).model_dump(
            mode="json"
        )
        runner = _FakeRunner(fake_result)
        FMIValidationEngine.configure_modal_runner(runner)

        engine = FMIValidationEngine(config={"inputs": {"u_in": 1.0}})
        result = engine.validate(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
        )

        self.assertTrue(result.passed)
        self.assertTrue(runner.calls)

    def test_fmi_engine_rejects_missing_fmu(self):
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            allowed_file_types=[SubmissionFileType.BINARY],
        )
        _prime_modal_cache_fake()
        validator = create_fmi_validator(
            org=org, project=None, name="has-fmu", upload=_fake_fmu()
        )
        validator.fmu_model = None
        validator.is_system = True
        validator.save(update_fields=["fmu_model", "is_system"])
        ruleset = Ruleset.objects.create(
            org=org,
            user=None,
            name="FMI Rules",
            ruleset_type=RulesetType.FMI,
            version="1",
            rules_text="{}",
        )
        submission = SubmissionFactory(
            file_type=SubmissionFileType.BINARY,
            org=org,
            workflow=workflow,
            project=workflow.project,
        )
        engine = FMIValidationEngine(config={})
        result = engine.validate(
            validator=validator, submission=submission, ruleset=ruleset
        )
        self.assertFalse(result.passed)
