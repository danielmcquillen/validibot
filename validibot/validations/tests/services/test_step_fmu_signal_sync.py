"""
Tests for step-level FMU signal synchronization.

When a user uploads an FMU to a workflow step, the system creates
``SignalDefinition`` and ``StepSignalBinding`` rows from the
introspected FMU variables. This test suite verifies the sync function
handles all lifecycle scenarios: initial upload, re-upload with changed
variables, removal, and edge cases like slug collisions.

The sync function is the step-level counterpart to the library-validator
flow in ``fmu._persist_variables()``. Library validators own signals via
the ``validator`` FK; step-level uploads own them via the
``workflow_step`` FK.
"""

from __future__ import annotations

from django.test import TestCase

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding
from validibot.validations.services.fmu_signals import clear_step_fmu_signals
from validibot.validations.services.fmu_signals import sync_step_fmu_signals
from validibot.workflows.tests.factories import WorkflowStepFactory


def _make_fmu_var(
    name: str,
    causality: str = "input",
    value_type: str = "Real",
    unit: str = "",
    description: str = "",
) -> dict:
    """Build a minimal FMU variable dict for testing.

    These dicts mirror the structure returned by FMU introspection and
    are passed to ``sync_step_fmu_signals()`` to create
    ``SignalDefinition`` rows.
    """
    return {
        "name": name,
        "causality": causality,
        "variability": "continuous",
        "value_reference": 1,
        "value_type": value_type,
        "unit": unit,
        "description": description,
        "label": "",
    }


class SyncStepFMUSignalsTests(TestCase):
    """Tests for the sync_step_fmu_signals() service function."""

    def test_creates_signal_definitions_for_inputs_and_outputs(self):
        """Input and output FMU variables should each get a SignalDefinition
        with the correct direction and step FK ownership.
        """
        step = WorkflowStepFactory()
        variables = [
            _make_fmu_var("T_outdoor", causality="input"),
            _make_fmu_var("Q_heating", causality="output"),
        ]

        sync_step_fmu_signals(step, variables)

        sigs = SignalDefinition.objects.filter(workflow_step=step)
        self.assertEqual(sigs.count(), 2)

        input_sig = sigs.get(direction=SignalDirection.INPUT)
        self.assertEqual(input_sig.contract_key, "t_outdoor")
        self.assertEqual(input_sig.native_name, "T_outdoor")
        self.assertEqual(input_sig.origin_kind, SignalOriginKind.FMU)

        output_sig = sigs.get(direction=SignalDirection.OUTPUT)
        self.assertEqual(output_sig.contract_key, "q_heating")
        self.assertEqual(output_sig.native_name, "Q_heating")

    def test_creates_bindings_for_input_signals_only(self):
        """Input variables should get a StepSignalBinding with the variable
        name as source_data_path. Output variables should not get bindings
        (they're produced, not consumed).
        """
        step = WorkflowStepFactory()
        variables = [
            _make_fmu_var("T_outdoor", causality="input"),
            _make_fmu_var("Q_heating", causality="output"),
        ]

        sync_step_fmu_signals(step, variables)

        bindings = StepSignalBinding.objects.filter(workflow_step=step)
        self.assertEqual(bindings.count(), 1)

        binding = bindings.first()
        self.assertEqual(binding.source_scope, BindingSourceScope.SUBMISSION_PAYLOAD)
        self.assertEqual(binding.source_data_path, "")
        self.assertTrue(binding.is_required)

    def test_skips_parameter_variables(self):
        """FMU variables with causality 'parameter' or 'local' should not
        create SignalDefinition rows — they're internal to the FMU model.
        """
        step = WorkflowStepFactory()
        variables = [
            _make_fmu_var("T_outdoor", causality="input"),
            _make_fmu_var("some_param", causality="parameter"),
            _make_fmu_var("internal_state", causality="local"),
        ]

        sync_step_fmu_signals(step, variables)

        sigs = SignalDefinition.objects.filter(workflow_step=step)
        self.assertEqual(sigs.count(), 1)
        self.assertEqual(sigs.first().native_name, "T_outdoor")

    def test_reupload_preserves_matching_variables(self):
        """Re-uploading an FMU with overlapping variable names should update
        existing SignalDefinition rows, not create duplicates.
        """
        step = WorkflowStepFactory()

        # First upload
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input", unit="K"),
            ],
        )
        self.assertEqual(
            SignalDefinition.objects.filter(workflow_step=step).count(),
            1,
        )
        first_sig = SignalDefinition.objects.get(workflow_step=step)
        first_pk = first_sig.pk

        # Re-upload with same variable but different unit
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input", unit="degC"),
            ],
        )

        sigs = SignalDefinition.objects.filter(workflow_step=step)
        self.assertEqual(sigs.count(), 1)
        updated_sig = sigs.first()
        # Same row, updated in place
        self.assertEqual(updated_sig.pk, first_pk)
        self.assertEqual(updated_sig.unit, "degC")

    def test_reupload_removes_deleted_variables(self):
        """When a new FMU has different variables, signals from the old FMU
        should be deleted. This is important for keeping the signal model
        consistent with the actual FMU.
        """
        step = WorkflowStepFactory()

        # First upload: two variables
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input"),
                _make_fmu_var("Q_old_output", causality="output"),
            ],
        )
        self.assertEqual(
            SignalDefinition.objects.filter(workflow_step=step).count(),
            2,
        )

        # Re-upload: only one variable, the other is gone
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input"),
            ],
        )

        sigs = SignalDefinition.objects.filter(workflow_step=step)
        self.assertEqual(sigs.count(), 1)
        self.assertEqual(sigs.first().native_name, "T_outdoor")

    def test_clear_step_fmu_signals_removes_all(self):
        """Clearing signals should remove all FMU-origin signals and their
        bindings from the step. Called when the user removes the FMU.
        """
        step = WorkflowStepFactory()
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input"),
                _make_fmu_var("Q_heating", causality="output"),
            ],
        )
        self.assertEqual(
            SignalDefinition.objects.filter(workflow_step=step).count(),
            2,
        )

        clear_step_fmu_signals(step)

        self.assertEqual(
            SignalDefinition.objects.filter(workflow_step=step).count(),
            0,
        )
        self.assertEqual(
            StepSignalBinding.objects.filter(workflow_step=step).count(),
            0,
        )

    def test_provider_binding_and_metadata_populated(self):
        """The provider_binding JSON should capture FMU-specific properties
        (causality) and metadata should capture variability, value_reference,
        and value_type for downstream use by the resolution engine.
        """
        step = WorkflowStepFactory()
        sync_step_fmu_signals(
            step,
            [
                {
                    "name": "T_zone",
                    "causality": "output",
                    "variability": "continuous",
                    "value_reference": 42,
                    "value_type": "Real",
                    "unit": "K",
                    "description": "Zone temperature",
                    "label": "Zone Temp",
                },
            ],
        )

        sig = SignalDefinition.objects.get(workflow_step=step)
        self.assertEqual(sig.provider_binding["causality"], "output")
        self.assertEqual(sig.metadata["value_reference"], 42)
        self.assertEqual(sig.metadata["variability"], "continuous")
        self.assertEqual(sig.unit, "K")
        self.assertEqual(sig.description, "Zone temperature")

    def test_data_type_mapping(self):
        """FMU value types should map to the correct signal data types:
        Real/Integer → number, Boolean → boolean, String → string.
        """
        step = WorkflowStepFactory()
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("real_var", causality="input", value_type="Real"),
                _make_fmu_var("bool_var", causality="input", value_type="Boolean"),
                _make_fmu_var("str_var", causality="input", value_type="String"),
            ],
        )

        sigs = {
            s.native_name: s
            for s in SignalDefinition.objects.filter(workflow_step=step)
        }
        self.assertEqual(sigs["real_var"].data_type, "number")
        self.assertEqual(sigs["bool_var"].data_type, "boolean")
        self.assertEqual(sigs["str_var"].data_type, "string")

    def test_renamed_variable_creates_new_signal(self):
        """When an FMU variable is renamed (e.g., T_outdoor → T_ambient),
        the old signal is deleted and a new one is created. This means
        any assertions targeting the old contract_key will break.

        This is a known limitation deferred to Phase 6, when assertion
        targets migrate from ValidatorCatalogEntry FK to SignalDefinition
        FK. At that point, value_reference-based matching can be used to
        preserve stable contract_keys across renames.
        """
        step = WorkflowStepFactory()

        # First upload: T_outdoor
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input"),
            ],
        )
        old_sig = SignalDefinition.objects.get(workflow_step=step)
        self.assertEqual(old_sig.contract_key, "t_outdoor")

        # Re-upload: same variable renamed to T_ambient
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("T_ambient", causality="input"),
            ],
        )

        # Old signal is gone, new one exists
        sigs = list(SignalDefinition.objects.filter(workflow_step=step))
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].contract_key, "t_ambient")
        self.assertNotEqual(sigs[0].pk, old_sig.pk)

    def test_reupload_preserves_assertion_targets_on_signal_deletion(self):
        """When an FMU re-upload removes a variable that has assertions
        targeting it, the assertion's target_data_path should be set to
        the old contract_key so it remains valid under the XOR constraint.
        Without this fallback, SET_NULL on the FK would leave all three
        target fields empty, violating the database constraint.
        """
        from validibot.validations.tests.factories import RulesetAssertionFactory
        from validibot.validations.tests.factories import RulesetFactory

        step = WorkflowStepFactory()

        # Create signal and assertion targeting it
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("Q_old", causality="output"),
            ],
        )
        sig = SignalDefinition.objects.get(
            workflow_step=step,
            contract_key="q_old",
        )

        # Create a ruleset and assertion targeting this signal
        ruleset = RulesetFactory()
        assertion = RulesetAssertionFactory(
            ruleset=ruleset,
            target_signal_definition=sig,
            target_data_path="",
        )

        # Re-upload without Q_old → signal gets deleted
        sync_step_fmu_signals(
            step,
            [
                _make_fmu_var("Q_new", causality="output"),
            ],
        )

        # Assertion should have been migrated to target_data_path fallback
        assertion.refresh_from_db()
        self.assertIsNone(assertion.target_signal_definition)
        self.assertEqual(assertion.target_data_path, "q_old")
