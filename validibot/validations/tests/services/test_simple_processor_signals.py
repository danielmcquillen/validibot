from django.test import TestCase

from validibot.validations.constants import StepStatus
from validibot.validations.engines.base import AssertionStats
from validibot.validations.engines.base import ValidationResult
from unittest.mock import patch

from validibot.validations.services.step_processor.simple import (
    SimpleValidationProcessor,
)
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


class SimpleProcessorSignalsTests(TestCase):
    """Tests for signal persistence in the simple validation processor."""

    def test_simple_processor_stores_signals_in_step_and_summary(self):
        """Signals from simple engines should persist to step output and summary."""
        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            status=StepStatus.RUNNING,
        )

        signals = {"output_temp": 18.5}

        class FakeEngine:
            """Minimal engine stub returning signals for simple processing."""

            def validate(self, **_kwargs):
                return ValidationResult(
                    passed=True,
                    issues=[],
                    assertion_stats=AssertionStats(),
                    signals=signals,
                    stats={},
                )

        def fake_get_engine(_validation_type):
            return FakeEngine

        with patch(
            "validibot.validations.engines.registry.get",
            side_effect=fake_get_engine,
        ):
            processor = SimpleValidationProcessor(run, step_run)
            processor.execute()

        step_run.refresh_from_db()
        run.refresh_from_db()

        self.assertEqual(step_run.output.get("signals"), signals)
        step_key = str(step_run.id)
        self.assertEqual(
            run.summary.get("steps", {}).get(step_key, {}).get("signals"),
            signals,
        )
