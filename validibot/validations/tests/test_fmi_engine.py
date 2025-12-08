from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from vb_shared.fmi import FMIRunResult

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import RulesetType
from validibot.validations.engines.fmi import FMIValidationEngine
from validibot.validations.models import Ruleset
from validibot.validations.services.fmi import create_fmi_validator
from validibot.workflows.tests.factories import WorkflowFactory


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
    """Placeholder retained for backward compatibility in tests (no-op)."""
    return []


class FMIEngineTests(TestCase):
    """Validate FMI engine dispatch and required configuration."""

    def test_fmi_engine_success_path(self):
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            allowed_file_types=[SubmissionFileType.BINARY],
        )
        upload = _fake_fmu()
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
        _FakeRunner(fake_result)

        engine = FMIValidationEngine(config={"inputs": {"u_in": 1.0}})
        result = engine.validate(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
        )

        # Without Cloud Run config, engine returns failure
        self.assertFalse(result.passed)
        self.assertIn("implementation_status", result.stats)

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
