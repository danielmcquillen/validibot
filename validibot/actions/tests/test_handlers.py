"""
Tests for step handlers and the dispatcher logic in execute_workflow_step.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from validibot.actions.handlers import ValidatorStepHandler
from validibot.actions.protocols import RunContext
from validibot.actions.protocols import StepResult
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base import ValidationResult
from validibot.workflows.tests.factories import WorkflowStepFactory


class TestValidatorStepHandler:
    """Tests for ValidatorStepHandler."""

    def test_returns_error_when_step_has_no_validator(self):
        """Handler should return failed StepResult when step has no validator."""
        handler = ValidatorStepHandler()
        context = RunContext(
            validation_run=MagicMock(),
            step=MagicMock(validator=None),
            downstream_signals={},
        )

        result = handler.execute(context)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "no validator configured" in result.issues[0].message.lower()

    @pytest.mark.django_db
    def test_returns_error_for_unsupported_file_type(self):
        """Handler should fail when submission file type is not supported."""
        validator = ValidatorFactory()
        validator.supports_file_type = MagicMock(return_value=False)

        run = MagicMock()
        run.submission = MagicMock(file_type="unsupported")

        step = MagicMock(validator=validator)

        handler = ValidatorStepHandler()
        context = RunContext(
            validation_run=run,
            step=step,
            downstream_signals={},
        )

        result = handler.execute(context)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "unsupported" in result.issues[0].message.lower()
        assert result.issues[0].code == "unsupported_file_type"

    def test_returns_error_when_validator_not_found(self):
        """Handler should fail gracefully when validator class cannot be loaded."""
        validator = MagicMock()
        validator.validation_type = "nonexistent_type"
        validator.supports_file_type = MagicMock(return_value=True)

        run = MagicMock()
        run.submission = MagicMock(file_type="json")

        step = MagicMock(validator=validator)

        handler = ValidatorStepHandler()
        context = RunContext(
            validation_run=run,
            step=step,
            downstream_signals={},
        )

        result = handler.execute(context)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "failed to load" in result.issues[0].message.lower()


class TestExecuteWorkflowStepDispatcher:
    """Tests for the dispatcher logic in execute_workflow_step."""

    @pytest.mark.django_db
    def test_dispatches_to_validator_handler_when_step_has_validator(
        self,
        monkeypatch,
    ):
        """Should use ValidatorStepHandler when step has a validator."""
        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow)

        mock_result = StepResult(passed=True, issues=[], stats={"test": True})

        def mock_execute(self, context):
            return mock_result

        monkeypatch.setattr(ValidatorStepHandler, "execute", mock_execute)

        service = ValidationRunService()
        result = service.execute_workflow_step(step=step, validation_run=run)

        assert isinstance(result, ValidationResult)
        assert result.passed is True
        assert result.stats.get("test") is True

    @pytest.mark.django_db
    def test_returns_failed_result_when_step_has_no_handler(self):
        """Should return failed result when step has no validator or action."""
        run = ValidationRunFactory()
        step = MagicMock(validator=None, action=None, name="orphan_step")

        service = ValidationRunService()
        result = service.execute_workflow_step(step=step, validation_run=run)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "no validator or action" in result.issues[0].message.lower()

    @pytest.mark.django_db
    def test_returns_failed_result_when_action_handler_not_registered(self):
        """Should return failed result when action type has no handler."""
        run = ValidationRunFactory()

        mock_action = MagicMock()
        mock_action.definition.type = "unregistered_action_type"

        step = MagicMock(validator=None, action=mock_action, name="action_step")

        service = ValidationRunService()
        result = service.execute_workflow_step(step=step, validation_run=run)

        assert result.passed is False
        assert len(result.issues) == 1
        assert "no handler registered" in result.issues[0].message.lower()
