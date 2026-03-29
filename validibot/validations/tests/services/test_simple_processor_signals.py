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
        # Signals are namespaced under the stable step_key slug (not
        # the ephemeral step_run UUID) so cross-step assertions can
        # reference them by a stable, author-visible identifier.
        self.assertEqual(
            run.summary.get("steps", {}).get(step.step_key, {}).get("signals"),
            signals,
        )
