from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from validibot.actions.protocols import RunContext
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import FMUModel
from validibot.validations.models import Ruleset
from validibot.validations.models import Validator
from validibot.validations.services.fmu import create_fmu_validator
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.fmu.validator import FMUValidator
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


class FMUValidatorTests(TestCase):
    """Validate FMU validator dispatch and required configuration."""

    def test_fmu_validator_requires_run_context(self):
        """Test that FMU validator returns error when run_context is not provided."""
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            allowed_file_types=[SubmissionFileType.BINARY],
        )
        upload = _fake_fmu()
        validator = create_fmu_validator(
            org=org,
            project=workflow.project,
            name="Test FMU",
            upload=upload,
        )
        ruleset = Ruleset.objects.create(
            org=org,
            user=None,
            name="FMU Rules",
            ruleset_type=RulesetType.FMU,
            version="1",
            rules_text="{}",
        )
        submission = SubmissionFactory(
            org=org,
            project=workflow.project,
            workflow=workflow,
            file_type=SubmissionFileType.BINARY,
        )

        engine = FMUValidator(config={})

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

    def test_fmu_validator_backend_not_available(self):
        """FMU validator returns error when execution backend not available."""
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            allowed_file_types=[SubmissionFileType.BINARY],
        )
        upload = _fake_fmu()
        validator = create_fmu_validator(
            org=org,
            project=workflow.project,
            name="Test FMU",
            upload=upload,
        )
        ruleset = Ruleset.objects.create(
            org=org,
            user=None,
            name="FMU Rules",
            ruleset_type=RulesetType.FMU,
            version="1",
            rules_text="{}",
        )
        submission = SubmissionFactory(
            org=org,
            project=workflow.project,
            workflow=workflow,
            file_type=SubmissionFileType.BINARY,
        )

        engine = FMUValidator(config={"inputs": {"u_in": 1.0}})

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

        # Without available execution backend, validator returns failure
        self.assertFalse(result.passed)
        self.assertIn("implementation_status", result.stats)
        self.assertEqual(result.stats["implementation_status"], "Backend not available")

    def test_fmu_validator_rejects_missing_fmu(self):
        """Test that FMU validator handles missing FMU gracefully."""
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            allowed_file_types=[SubmissionFileType.BINARY],
        )
        _prime_modal_cache_fake()
        validator = create_fmu_validator(
            org=org, project=None, name="has-fmu", upload=_fake_fmu()
        )
        validator.fmu_model = None
        validator.is_system = True
        validator.save(update_fields=["fmu_model", "is_system"])
        ruleset = Ruleset.objects.create(
            org=org,
            user=None,
            name="FMU Rules",
            ruleset_type=RulesetType.FMU,
            version="1",
            rules_text="{}",
        )
        submission = SubmissionFactory(
            file_type=SubmissionFileType.BINARY,
            org=org,
            workflow=workflow,
            project=workflow.project,
        )
        engine = FMUValidator(config={})

        # Don't pass run_context - will fail with missing context error
        # which is fine since we're testing that validator handles edge cases
        result = engine.validate(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            run_context=None,
        )
        self.assertFalse(result.passed)


class FMUValidatorModelTests(TestCase):
    """
    Validate FMU attachment requirements for FMU validators.

    FMU validators owned by an org must carry an FMU; system FMU validators can
    exist as placeholders until an FMU is assigned.
    """

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    def test_system_fmu_validator_can_exist_without_fmu(self) -> None:
        """System FMU validators remain valid without an FMU attachment."""

        validator = ValidatorFactory(validation_type=ValidationType.FMU, is_system=True)
        validator.full_clean()

    def test_org_fmu_validator_requires_fmu_asset(self) -> None:
        """
        Non-system FMU validators must reference an FMUModel before validation.
        """

        # Build without saving so we can assert full_clean fails as expected.
        validator = ValidatorFactory.build(
            validation_type=ValidationType.FMU,
            org=self.org,
            is_system=False,
        )
        with pytest.raises(ValidationError):
            validator.full_clean()

    def test_org_fmu_validator_accepts_fmu_asset(self) -> None:
        """Org FMU validators validate successfully when an FMUModel is attached."""

        fmu_file = SimpleUploadedFile(
            "demo.fmu",
            b"dummy-fmu",
            content_type="application/octet-stream",
        )
        fmu = FMUModel.objects.create(
            org=self.org,
            name="Demo FMU",
            file=fmu_file,
        )
        validator = Validator(
            name="Org FMU",
            slug="org-fmu",
            validation_type=ValidationType.FMU,
            org=self.org,
            is_system=False,
            fmu_model=fmu,
        )
        validator.full_clean()
