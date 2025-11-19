from __future__ import annotations

import io
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.validations.constants import RulesetType, ValidationType
from simplevalidations.validations.engines.fmi import FMIValidationEngine
from simplevalidations.validations.models import Ruleset
from simplevalidations.validations.services.fmi import create_fmi_validator
from simplevalidations.workflows.tests.factories import WorkflowFactory
from sv_shared.fmi import FMIRunResult


def _fake_fmu() -> SimpleUploadedFile:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="demo">
  <ModelVariables>
    <ScalarVariable name="u_in" causality="input" valueReference="1"><Real/></ScalarVariable>
    <ScalarVariable name="y_out" causality="output" valueReference="2"><Real/></ScalarVariable>
  </ModelVariables>
</fmiModelDescription>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("modelDescription.xml", xml)
    buf.seek(0)
    return SimpleUploadedFile("demo.fmu", buf.getvalue(), content_type="application/octet-stream")


class FMIEngineTests(TestCase):
    """Validate FMI engine Modal dispatch and required configuration."""

    def tearDown(self):
        FMIValidationEngine.configure_modal_runner(None)

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

        fake_result = FMIRunResult.success(outputs={"y_out": 3.0}).model_dump(mode="json")
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
        validator = create_fmi_validator(org=org, project=None, name="has-fmu", upload=_fake_fmu())
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
        result = engine.validate(validator=validator, submission=submission, ruleset=ruleset)
        self.assertFalse(result.passed)
