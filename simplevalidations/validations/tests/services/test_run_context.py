"""
Tests for run-aware validator execution and downstream signal exposure.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.engines.basic import BasicValidatorEngine
from simplevalidations.validations.services.validation_run import ValidationRunService
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory


class _DummyRunAwareEngine(BaseValidatorEngine):
    last_context = None
    calls: list[tuple] = []

    def validate_with_run(self, validator, submission, ruleset, run, step):
        self.__class__.last_context = self.run_context
        self.__class__.calls.append(
            ("validate_with_run", run.id if run else None, step.id if step else None),
        )
        return ValidationResult(passed=True, issues=[])

    def validate(self, validator, submission, ruleset):
        self.__class__.calls.append(("validate", None, None))
        return ValidationResult(passed=True, issues=[])


@pytest.mark.django_db
def test_run_validator_engine_prefers_validate_with_run(monkeypatch):
    """run_validator_engine should call validate_with_run when available."""
    _DummyRunAwareEngine.calls.clear()
    run = ValidationRunFactory()
    step = WorkflowStepFactory(workflow=run.workflow, validator=ValidatorFactory())
    service = ValidationRunService()

    monkeypatch.setattr(
        "simplevalidations.validations.services.validation_run.get_validator_class",
        lambda _vtype: _DummyRunAwareEngine,
    )

    result = service.run_validator_engine(
        validator=step.validator,
        submission=run.submission,
        ruleset=None,
        config={},
        validation_run=run,
        step=step,
    )

    assert isinstance(result, ValidationResult)
    assert _DummyRunAwareEngine.calls == [
        ("validate_with_run", run.id, step.id),
    ]
    assert _DummyRunAwareEngine.last_context.validation_run == run
    assert _DummyRunAwareEngine.last_context.workflow_step == step


@pytest.mark.django_db
def test_build_cel_context_exposes_downstream_signals():
    """CEL context should include signals from prior steps under steps.<id>.signals."""
    validator = ValidatorFactory(validation_type=ValidationType.BASIC)
    engine = BasicValidatorEngine()
    engine.run_context.validation_run = SimpleNamespace(
        summary={"steps": {"10": {"signals": {"output_temp": 18.5}}}},
    )

    context = engine._build_cel_context({"input": 1}, validator)  # noqa: SLF001

    assert context["payload"] == {"input": 1}
    assert context["steps"]["10"]["signals"]["output_temp"] == 18.5 # noqa: PLR2004
