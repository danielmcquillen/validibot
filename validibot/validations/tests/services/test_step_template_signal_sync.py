"""
Tests for EnergyPlus template variable signal synchronization.

When an author uploads an IDF template to a workflow step, the scanner
extracts ``$VARIABLE_NAME`` placeholders. These are passed to
``sync_step_template_signals()`` which persists them as
``SignalDefinition`` and ``StepSignalBinding`` rows.

Template variables are always input signals (``direction=INPUT``,
``origin_kind=TEMPLATE``) — they represent values the submitter provides
to parameterize the IDF before simulation.
"""

from __future__ import annotations

from django.test import TestCase

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding
from validibot.validations.services.template_signals import clear_step_template_signals
from validibot.validations.services.template_signals import sync_step_template_signals
from validibot.workflows.tests.factories import WorkflowStepFactory


def _make_template_var(
    name: str,
    description: str = "",
    default: str | float | None = None,
    units: str = "",
    variable_type: str = "text",
    **kwargs,
) -> dict:
    """Build a minimal template variable dict for testing.

    These dicts mirror the structure returned by the IDF scanner and
    are passed to ``sync_step_template_signals()`` to create
    ``SignalDefinition`` rows.
    """
    return {
        "name": name,
        "description": description,
        "default": default,
        "units": units,
        "variable_type": variable_type,
        "min_value": kwargs.get("min_value"),
        "max_value": kwargs.get("max_value"),
        "min_exclusive": kwargs.get("min_exclusive", False),
        "max_exclusive": kwargs.get("max_exclusive", False),
        "choices": kwargs.get("choices", []),
    }


class SyncStepTemplateSignalsTests(TestCase):
    """Tests for the sync_step_template_signals() service function."""

    def test_creates_input_signal_definitions(self):
        """Each template variable should create a SignalDefinition with
        direction=INPUT and origin_kind=TEMPLATE, owned by the step.
        """
        step = WorkflowStepFactory()
        variables = [
            _make_template_var("U_FACTOR", description="Wall U-Factor"),
            _make_template_var("HEATING_SETPOINT"),
        ]

        sync_step_template_signals(step, variables)

        sigs = SignalDefinition.objects.filter(workflow_step=step)
        self.assertEqual(sigs.count(), 2)

        for sig in sigs:
            self.assertEqual(sig.direction, SignalDirection.INPUT)
            self.assertEqual(sig.origin_kind, SignalOriginKind.TEMPLATE)
            self.assertIsNone(sig.validator)

    def test_creates_bindings_with_default_values(self):
        """Template variables with defaults should create StepSignalBinding
        rows with default_value set. Variables without defaults should be
        marked as required (is_required=True).
        """
        step = WorkflowStepFactory()
        variables = [
            _make_template_var("U_FACTOR", default=0.35),
            _make_template_var("ZONE_NAME"),  # no default → required
        ]

        sync_step_template_signals(step, variables)

        bindings = StepSignalBinding.objects.filter(
            workflow_step=step,
        ).select_related("signal_definition")

        self.assertEqual(bindings.count(), 2)

        u_factor_binding = bindings.get(
            signal_definition__contract_key="u_factor",
        )
        self.assertEqual(u_factor_binding.default_value, 0.35)
        self.assertFalse(u_factor_binding.is_required)
        self.assertEqual(
            u_factor_binding.source_scope,
            BindingSourceScope.SUBMISSION_PAYLOAD,
        )

        zone_binding = bindings.get(
            signal_definition__contract_key="zone_name",
        )
        self.assertIsNone(zone_binding.default_value)
        self.assertTrue(zone_binding.is_required)

    def test_metadata_captures_validation_constraints(self):
        """Template variable metadata should include type, min/max,
        and choices — needed by the merge/validate pipeline to enforce
        constraints before substitution.
        """
        step = WorkflowStepFactory()
        sync_step_template_signals(
            step,
            [
                _make_template_var(
                    "GLAZING_U",
                    variable_type="number",
                    min_value=0.1,
                    max_value=5.0,
                    units="W/m2-K",
                ),
            ],
        )

        sig = SignalDefinition.objects.get(workflow_step=step)
        self.assertEqual(sig.unit, "W/m2-K")
        self.assertEqual(sig.metadata["variable_type"], "number")
        self.assertEqual(sig.metadata["min_value"], 0.1)
        self.assertEqual(sig.metadata["max_value"], 5.0)

    def test_choice_variable_type(self):
        """Choice-type template variables should store allowed values in
        metadata and map to STRING data_type.
        """
        step = WorkflowStepFactory()
        sync_step_template_signals(
            step,
            [
                _make_template_var(
                    "SYSTEM_TYPE",
                    variable_type="choice",
                    choices=["VAV", "DOAS", "Fan Coil"],
                ),
            ],
        )

        sig = SignalDefinition.objects.get(workflow_step=step)
        self.assertEqual(sig.data_type, "string")
        self.assertEqual(sig.metadata["choices"], ["VAV", "DOAS", "Fan Coil"])

    def test_reupload_preserves_matching_variables(self):
        """Re-uploading a template with the same variable names should
        update existing rows, not create duplicates.
        """
        step = WorkflowStepFactory()

        sync_step_template_signals(
            step,
            [
                _make_template_var("U_FACTOR", units="W/m2-K"),
            ],
        )
        first_pk = SignalDefinition.objects.get(workflow_step=step).pk

        sync_step_template_signals(
            step,
            [
                _make_template_var("U_FACTOR", units="Btu/h-ft2-F"),
            ],
        )

        sigs = SignalDefinition.objects.filter(workflow_step=step)
        self.assertEqual(sigs.count(), 1)
        self.assertEqual(sigs.first().pk, first_pk)
        self.assertEqual(sigs.first().unit, "Btu/h-ft2-F")

    def test_reupload_removes_deleted_variables(self):
        """When the new template has different variables, signals from
        the old template should be deleted.
        """
        step = WorkflowStepFactory()

        sync_step_template_signals(
            step,
            [
                _make_template_var("OLD_VAR"),
                _make_template_var("KEPT_VAR"),
            ],
        )
        self.assertEqual(
            SignalDefinition.objects.filter(workflow_step=step).count(),
            2,
        )

        sync_step_template_signals(
            step,
            [
                _make_template_var("KEPT_VAR"),
            ],
        )

        sigs = SignalDefinition.objects.filter(workflow_step=step)
        self.assertEqual(sigs.count(), 1)
        self.assertEqual(sigs.first().contract_key, "kept_var")

    def test_clear_removes_all_template_signals(self):
        """Clearing should remove all template-origin signals and their
        bindings. Called when the author switches to direct IDF mode.
        """
        step = WorkflowStepFactory()
        sync_step_template_signals(
            step,
            [
                _make_template_var("U_FACTOR"),
                _make_template_var("HEATING_SETPOINT"),
            ],
        )
        self.assertEqual(
            SignalDefinition.objects.filter(workflow_step=step).count(),
            2,
        )

        clear_step_template_signals(step)

        self.assertEqual(
            SignalDefinition.objects.filter(workflow_step=step).count(),
            0,
        )
        self.assertEqual(
            StepSignalBinding.objects.filter(workflow_step=step).count(),
            0,
        )

    def test_does_not_interfere_with_fmu_signals(self):
        """Template signals (origin_kind=TEMPLATE) and FMU signals
        (origin_kind=FMU) on the same step should coexist without
        interference. Clearing one type should not affect the other.
        """
        step = WorkflowStepFactory()

        # Create an FMU signal on this step
        from validibot.validations.services.fmu_signals import sync_step_fmu_signals

        sync_step_fmu_signals(
            step,
            [
                {
                    "name": "T_outdoor",
                    "causality": "input",
                    "variability": "continuous",
                    "value_reference": 1,
                    "value_type": "Real",
                    "unit": "K",
                    "description": "",
                    "label": "",
                },
            ],
        )

        # Create a template signal
        sync_step_template_signals(
            step,
            [
                _make_template_var("U_FACTOR"),
            ],
        )

        # Both should exist
        self.assertEqual(
            SignalDefinition.objects.filter(workflow_step=step).count(),
            2,
        )

        # Clearing template signals should not affect FMU signals
        clear_step_template_signals(step)
        remaining = SignalDefinition.objects.filter(workflow_step=step)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.first().origin_kind, SignalOriginKind.FMU)
