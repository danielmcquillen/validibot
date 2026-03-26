"""Tests for StepOrchestrator internal methods.

Covers step lifecycle (_start_step_run) idempotency and
the _record_step_result normalization precondition.
"""

from __future__ import annotations

import sys
from collections import Counter
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import ActionFailureMode
from validibot.actions.constants import CredentialActionType
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.step_orchestrator import StepOrchestrator
from validibot.validations.services.step_processor.result import StepProcessingResult
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

# ---------- _start_step_run ----------


@pytest.mark.django_db
class TestStartStepRun:
    """Test _start_step_run idempotency and retry behavior."""

    def setup_method(self):
        self.orchestrator = StepOrchestrator()
        self.run = ValidationRunFactory()
        self.wf_step = WorkflowStepFactory(workflow=self.run.workflow)

    def test_new_step_creates_running_step_run(self):
        """First call creates a new step run with RUNNING status."""
        step_run, should_execute = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert should_execute is True
        assert step_run.status == StepStatus.RUNNING
        assert step_run.started_at is not None
        assert step_run.step_order == (self.wf_step.order or 0)

    @pytest.mark.parametrize(
        "terminal_status",
        [
            StepStatus.PASSED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
        ],
    )
    def test_terminal_step_skips_execution(self, terminal_status):
        """A step that already finished returns should_execute=False."""
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=terminal_status,
        )

        step_run, should_execute = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert should_execute is False
        assert step_run.id == existing.id
        # Status should not have changed
        step_run.refresh_from_db()
        assert step_run.status == terminal_status

    def test_running_step_resets_timing(self):
        """A RUNNING step (crash recovery) gets started_at reset."""
        old_time = timezone.now() - timezone.timedelta(minutes=5)
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=StepStatus.RUNNING,
            started_at=old_time,
        )

        step_run, should_execute = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert should_execute is True
        assert step_run.id == existing.id
        step_run.refresh_from_db()
        # started_at should be reset to a recent time, not the old one
        assert step_run.started_at > old_time

    def test_running_step_clears_partial_findings(self):
        """A RUNNING step (crash recovery) has its findings cleared."""
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=StepStatus.RUNNING,
        )
        # Simulate partial findings from a crashed prior attempt
        ValidationFinding.objects.create(
            validation_run=self.run,
            validation_step_run=existing,
            severity=Severity.ERROR,
            message="stale finding from crashed attempt",
        )
        assert (
            ValidationFinding.objects.filter(
                validation_step_run=existing,
            ).count()
            == 1
        )

        step_run, should_execute = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert should_execute is True
        # Partial findings should be cleared
        assert (
            ValidationFinding.objects.filter(
                validation_step_run=step_run,
            ).count()
            == 0
        )

    def test_idempotent_on_second_new_call(self):
        """Two calls for the same (run, step) return the same step run."""
        step_run_1, _ = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )
        step_run_2, _ = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert step_run_1.id == step_run_2.id
        # Only one row should exist
        assert (
            ValidationStepRun.objects.filter(
                validation_run=self.run,
                workflow_step=self.wf_step,
            ).count()
            == 1
        )


@pytest.mark.django_db
class TestDeferredSignedCredentialIssuance:
    """Verify signed credentials are issued after run finalization."""

    def test_signed_credential_issues_after_run_is_succeeded(self):
        """Credential issuance must happen after the run reaches SUCCEEDED."""
        orchestrator = StepOrchestrator()
        run = ValidationRunFactory(status=ValidationRunStatus.PENDING)
        validator = ValidatorFactory()
        WorkflowStepFactory(
            workflow=run.workflow,
            order=10,
            validator=validator,
        )
        credential_definition = ActionDefinition.objects.create(
            slug="signed-credential",
            name="Signed credential",
            action_category=ActionCategoryType.CREDENTIAL,
            type=CredentialActionType.SIGNED_CREDENTIAL,
            is_active=True,
        )
        credential_action = Action.objects.create(
            definition=credential_definition,
            slug="credential-action",
            name="Credential action",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        credential_step = WorkflowStepFactory(
            workflow=run.workflow,
            order=20,
            validator=None,
            action=credential_action,
        )

        def fake_execute_validator_step(*, validation_run, step_run):
            finalized = orchestrator._finalize_step_run(
                step_run=step_run,
                status=StepStatus.PASSED,
                stats={},
                error=None,
            )
            return StepProcessingResult(
                passed=True,
                step_run=finalized,
                severity_counts=Counter(),
                total_findings=0,
                assertion_failures=0,
                assertion_total=0,
            )

        issued = SimpleNamespace(id=uuid4())
        fake_issue_credential = Mock(return_value=issued)
        fake_issuance_module = ModuleType("validibot_pro.credentials.issuance")
        fake_issuance_module.issue_credential = fake_issue_credential
        fake_issuance_module.CredentialIssuanceError = RuntimeError
        with (
            patch.object(
                orchestrator,
                "_execute_validator_step",
                side_effect=fake_execute_validator_step,
            ),
            patch.dict(
                sys.modules,
                {
                    "validibot_pro": ModuleType("validibot_pro"),
                    "validibot_pro.credentials": ModuleType(
                        "validibot_pro.credentials",
                    ),
                    "validibot_pro.credentials.issuance": fake_issuance_module,
                },
            ),
        ):
            result = orchestrator.execute_workflow_steps(run.id, run.user_id)

        run.refresh_from_db()
        step_run = ValidationStepRun.objects.get(
            validation_run=run,
            workflow_step=credential_step,
        )
        assert result.status == ValidationRunStatus.SUCCEEDED
        assert run.status == ValidationRunStatus.SUCCEEDED
        fake_issue_credential.assert_called_once_with(step_run)
        assert step_run.status == StepStatus.PASSED
        assert step_run.output["credential_issuance"] == "issued"
        assert step_run.output["credential_id"] == str(issued.id)

    def test_advisory_credential_failure_adds_warning_but_run_succeeds(self):
        """Advisory credential failures should warn without failing the run."""
        orchestrator = StepOrchestrator()
        run = ValidationRunFactory(status=ValidationRunStatus.PENDING)
        validator = ValidatorFactory()
        WorkflowStepFactory(
            workflow=run.workflow,
            order=10,
            validator=validator,
        )
        credential_definition = ActionDefinition.objects.create(
            slug="signed-credential-advisory",
            name="Signed credential",
            action_category=ActionCategoryType.CREDENTIAL,
            type=CredentialActionType.SIGNED_CREDENTIAL,
            is_active=True,
        )
        credential_action = Action.objects.create(
            definition=credential_definition,
            slug="credential-action-advisory",
            name="Credential action",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        credential_step = WorkflowStepFactory(
            workflow=run.workflow,
            order=20,
            validator=None,
            action=credential_action,
        )

        def fake_execute_validator_step(*, validation_run, step_run):
            finalized = orchestrator._finalize_step_run(
                step_run=step_run,
                status=StepStatus.PASSED,
                stats={},
                error=None,
            )
            return StepProcessingResult(
                passed=True,
                step_run=finalized,
                severity_counts=Counter(),
                total_findings=0,
                assertion_failures=0,
                assertion_total=0,
            )

        class FakeCredentialIssuanceError(Exception):
            """Stub exception matching the Pro issuance service contract."""

        fake_issue_credential = Mock(
            side_effect=FakeCredentialIssuanceError(
                "Signing backend is not configured.",
            ),
        )
        fake_issuance_module = ModuleType("validibot_pro.credentials.issuance")
        fake_issuance_module.issue_credential = fake_issue_credential
        fake_issuance_module.CredentialIssuanceError = FakeCredentialIssuanceError
        with (
            patch.object(
                orchestrator,
                "_execute_validator_step",
                side_effect=fake_execute_validator_step,
            ),
            patch.dict(
                sys.modules,
                {
                    "validibot_pro": ModuleType("validibot_pro"),
                    "validibot_pro.credentials": ModuleType(
                        "validibot_pro.credentials",
                    ),
                    "validibot_pro.credentials.issuance": fake_issuance_module,
                },
            ),
        ):
            result = orchestrator.execute_workflow_steps(run.id, run.user_id)

        run.refresh_from_db()
        step_run = ValidationStepRun.objects.get(
            validation_run=run,
            workflow_step=credential_step,
        )
        finding = ValidationFinding.objects.get(validation_step_run=step_run)

        assert result.status == ValidationRunStatus.SUCCEEDED
        assert run.status == ValidationRunStatus.SUCCEEDED
        assert step_run.status == StepStatus.FAILED
        assert step_run.output["credential_issuance"] == "failed"
        assert finding.severity == Severity.WARNING
        assert finding.code == "credential_issuance_failed"
