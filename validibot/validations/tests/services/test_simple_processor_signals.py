from unittest.mock import patch

from django.test import TestCase

from validibot.validations.constants import StepStatus
from validibot.validations.services.step_processor.simple import (
    SimpleValidationProcessor,
)
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.validators.base import AssertionStats
from validibot.validations.validators.base import ValidationResult
from validibot.workflows.tests.factories import WorkflowStepFactory


class SimpleProcessorSignalsTests(TestCase):
    """Tests for signal persistence in the simple validation processor."""

    def test_simple_processor_stores_signals_in_step_and_summary(self):
        """Signals from simple validators should persist to step output and summary."""
        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            status=StepStatus.RUNNING,
        )

        signals = {"output_temp": 18.5}

        class FakeValidator:
            """Minimal validator stub returning signals for simple processing."""

            def validate(self, **_kwargs):
                return ValidationResult(
                    passed=True,
                    issues=[],
                    assertion_stats=AssertionStats(),
                    signals=signals,
                    stats={},
                )

        def fake_get_validator(_validation_type):
            return FakeValidator

        with patch(
            "validibot.validations.validators.base.config.get_validator_class",
            side_effect=fake_get_validator,
        ):
            processor = SimpleValidationProcessor(run, step_run)
            processor.execute()

        step_run.refresh_from_db()
        run.refresh_from_db()

        self.assertEqual(step_run.output.get("signals"), signals)
        # Validator outputs are namespaced under the stable step_key
        # slug (not the ephemeral step_run UUID) and stored under the
        # "output" key so downstream CEL expressions can reference
        # them via steps.<step_key>.output.<name>.
        self.assertEqual(
            run.summary.get("steps", {}).get(step.step_key, {}).get("output"),
            signals,
        )


class ProcessorRunContextConstantsTests(TestCase):
    """Workflow Constants (``c.*``) must reach the RunContext the processor builds.

    ADR-2026-06-18. This is the regression guard for a real production bug: the
    main run loop routes *validator* steps through the processor path
    (``get_step_processor().execute()`` → ``_build_run_context()``), NOT the
    orchestrator's action/handler path where constants were being set. When
    ``_build_run_context()`` omitted ``workflow_constants``, ``c.*``/``const.*``
    silently evaluated against ``{}`` in real runs, while unit tests using
    hand-built ``RunContext(workflow_constants=...)`` still passed — the classic
    test-green / prod-broken trap. These tests assert the constants map at the
    exact seam where the bug lived, so it cannot come back unnoticed.

    ``_build_run_context`` lives on the shared base class, so exercising it via
    ``SimpleValidationProcessor`` covers the advanced processor too.
    """

    def _make_step_run(self, workflow):
        """Create a RUNNING step run for a step in ``workflow`` (test helper)."""
        run = ValidationRunFactory(workflow=workflow)
        step = WorkflowStepFactory(workflow=workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            status=StepStatus.RUNNING,
        )
        return run, step_run

    def test_build_run_context_includes_workflow_constants(self):
        """A workflow's constants appear in the processor-built RunContext.

        This is what makes ``c.energy_price`` resolvable in a real validator run
        rather than evaluating against an empty map. The value is the CEL-ready
        form: ``NUMBER`` is stored as the exact decimal string ``"0.40"`` but
        coerced to a ``double`` (``0.4``) for evaluation, since CEL has no
        decimal type (ADR-2026-06-18).
        """
        from validibot.workflows.constants import WorkflowConstantType
        from validibot.workflows.models import WorkflowConstant
        from validibot.workflows.tests.factories import WorkflowFactory

        workflow = WorkflowFactory()
        WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        run, step_run = self._make_step_run(workflow)

        run_context = SimpleValidationProcessor(run, step_run)._build_run_context()

        self.assertEqual(run_context.workflow_constants, {"energy_price": 0.4})

    def test_build_run_context_has_empty_constants_when_none_defined(self):
        """No constants → an empty map (no crash, no stray keys).

        The ``c``/``const`` namespace is always present in the context (so CEL
        never hits an undefined-variable error); it is simply empty when the
        workflow defines no constants.
        """
        from validibot.workflows.tests.factories import WorkflowFactory

        run, step_run = self._make_step_run(WorkflowFactory())

        run_context = SimpleValidationProcessor(run, step_run)._build_run_context()

        self.assertEqual(run_context.workflow_constants, {})
