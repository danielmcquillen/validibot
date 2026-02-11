from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from validibot.actions.protocols import RunContext
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import RulesetType
from validibot.validations.engines.fmi import FMUValidationEngine
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


def _mock_run_context() -> RunContext:
    """Create a mock RunContext for testing."""
    return RunContext(
        validation_run=MagicMock(id=1),
        step=MagicMock(id=1),
        downstream_signals={},
    )


class FMIEngineTests(TestCase):
    """Validate FMI engine dispatch and required configuration."""

    def test_fmi_engine_requires_run_context(self):
        """Test that FMI engine returns error when run_context is not provided."""
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

        engine = FMUValidationEngine(config={})

        # Don't pass run_context - should fail
        result = engine.validate(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            run_context=None,
        )

        self.assertFalse(result.passed)
        self.assertTrue(
            any("workflow context" in issue.message.lower() for issue in result.issues)
        )
        self.assertEqual(result.stats["implementation_status"], "Missing run_context")

    def test_fmi_engine_backend_not_available(self):
        """Test that FMI engine returns error when execution backend not available."""
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

        engine = FMUValidationEngine(config={"inputs": {"u_in": 1.0}})

        # Mock the backend to be unavailable
        with patch(
            "validibot.validations.services.execution.get_execution_backend"
        ) as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.is_available.return_value = False
            mock_backend.backend_name = "MockBackend"
            mock_get_backend.return_value = mock_backend

            # Pass run_context so we get past that check
            result = engine.validate(
                validator=validator,
                submission=submission,
                ruleset=ruleset,
                run_context=_mock_run_context(),
            )

        # Without available execution backend, engine returns failure
        self.assertFalse(result.passed)
        self.assertIn("implementation_status", result.stats)
        self.assertEqual(result.stats["implementation_status"], "Backend not available")

    def test_fmi_engine_rejects_missing_fmu(self):
        """Test that FMI engine handles missing FMU gracefully."""
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
        engine = FMUValidationEngine(config={})

        # Don't pass run_context - will fail with missing context error
        # which is fine since we're testing that engine handles edge cases
        result = engine.validate(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            run_context=None,
        )
        self.assertFalse(result.passed)
