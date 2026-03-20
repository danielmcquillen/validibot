"""
Tests for ensure_step_signal_bindings().

This function creates default StepSignalBinding rows for validator-owned
input signals that don't already have bindings on a given step. This
ensures the signal resolution engine activates instead of falling back
to legacy mode.

The function is called after step creation/update in save_workflow_step().
It only handles CATALOG-origin signals — FMU and TEMPLATE signals have
their own dedicated sync functions.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding
from validibot.validations.models import Validator
from validibot.validations.services.signal_bindings import ensure_step_signal_bindings
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import StepSignalBindingFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

# ── Core binding creation ────────────────────────────────────────────
# These tests verify that the function creates the right bindings for
# CATALOG-origin input signals owned by the step's validator.


class TestEnsureStepSignalBindings(TestCase):
    """Tests for the ensure_step_signal_bindings() service function."""

    def test_creates_bindings_for_input_signals(self):
        """CATALOG input signals owned by the validator should each get a
        StepSignalBinding so the resolution engine can map submission
        data to validator inputs instead of using the legacy fallback.
        """
        validator = ValidatorFactory()
        sig_a = SignalDefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            native_name="panel_area",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.CATALOG,
        )
        sig_b = SignalDefinitionFactory(
            validator=validator,
            contract_key="heating_setpoint",
            native_name="heating_setpoint",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_signal_bindings(step)

        self.assertEqual(count, 2)
        self.assertEqual(
            StepSignalBinding.objects.filter(workflow_step=step).count(),
            2,
        )

        binding_a = StepSignalBinding.objects.get(
            workflow_step=step,
            signal_definition=sig_a,
        )
        self.assertEqual(
            binding_a.source_scope,
            BindingSourceScope.SUBMISSION_PAYLOAD,
        )
        self.assertEqual(binding_a.source_data_path, "panel_area")
        self.assertTrue(binding_a.is_required)
        self.assertIsNone(binding_a.default_value)

        binding_b = StepSignalBinding.objects.get(
            workflow_step=step,
            signal_definition=sig_b,
        )
        self.assertEqual(binding_b.source_data_path, "heating_setpoint")

    def test_uses_system_validator_config_for_binding_defaults(self):
        """System-validator library signals should derive binding defaults
        from the validator config, not from provider_binding JSON.

        EnergyPlus input signals are declared as submission-metadata
        bindings in the config. New workflow steps must therefore get
        ``submission_metadata`` scope, the configured metadata key, and
        the optional/required semantics declared by the config.
        """
        call_command("sync_validators")
        validator = Validator.objects.get(slug="energyplus-idf-validator")
        step = WorkflowStepFactory(validator=validator)

        ensure_step_signal_bindings(step)

        signal = SignalDefinition.objects.get(
            validator=validator,
            contract_key="expected_floor_area_m2",
            direction=SignalDirection.INPUT,
        )
        binding = StepSignalBinding.objects.get(
            workflow_step=step,
            signal_definition=signal,
        )
        self.assertEqual(
            binding.source_scope,
            BindingSourceScope.SUBMISSION_METADATA,
        )
        self.assertEqual(binding.source_data_path, "floor_area_m2")
        self.assertFalse(binding.is_required)
        self.assertIsNone(binding.default_value)


# ── Filtering: only the right signals get bindings ───────────────────
# These tests verify that output signals and non-CATALOG signals are
# correctly excluded so we don't create spurious bindings.


class TestEnsureStepSignalBindingsFiltering(TestCase):
    """Tests that the function only creates bindings for the correct subset
    of signals (CATALOG-origin inputs)."""

    def test_skips_output_signals(self):
        """Output signals should not get bindings because they are produced
        by the validator, not consumed from submission data.
        """
        validator = ValidatorFactory()
        SignalDefinitionFactory(
            validator=validator,
            contract_key="energy_demand",
            direction=SignalDirection.OUTPUT,
            origin_kind=SignalOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_signal_bindings(step)

        self.assertEqual(count, 0)
        self.assertFalse(
            StepSignalBinding.objects.filter(workflow_step=step).exists(),
        )

    def test_skips_step_owned_signals(self):
        """FMU and TEMPLATE origin signals are step-owned and managed by
        their own sync functions, so ensure_step_signal_bindings should
        not create bindings for them.
        """
        validator = ValidatorFactory()
        # FMU-origin signal owned by the validator (unusual but possible
        # in test scenarios — the key filter is origin_kind, not owner FK).
        SignalDefinitionFactory(
            validator=validator,
            contract_key="fmu_temp",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.FMU,
        )
        SignalDefinitionFactory(
            validator=validator,
            contract_key="template_var",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.TEMPLATE,
        )
        step = WorkflowStepFactory(validator=validator)

        count = ensure_step_signal_bindings(step)

        self.assertEqual(count, 0)
        self.assertFalse(
            StepSignalBinding.objects.filter(workflow_step=step).exists(),
        )


# ── Idempotency and safety ───────────────────────────────────────────
# These tests verify that the function is safe to call repeatedly and
# does not overwrite existing bindings that may have been customised.


class TestEnsureStepSignalBindingsIdempotency(TestCase):
    """Tests for idempotency and preservation of existing bindings."""

    def test_idempotent(self):
        """Running twice should create bindings only on the first call.
        The second call should detect existing bindings and skip them,
        returning 0 new bindings created.
        """
        validator = ValidatorFactory()
        SignalDefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        first_count = ensure_step_signal_bindings(step)
        second_count = ensure_step_signal_bindings(step)

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 0)
        self.assertEqual(
            StepSignalBinding.objects.filter(workflow_step=step).count(),
            1,
        )

    def test_preserves_existing_bindings(self):
        """If a binding already exists with a custom source_data_path
        (e.g., set by the workflow author), ensure_step_signal_bindings
        must not overwrite it. This protects author customisations.
        """
        validator = ValidatorFactory()
        sig = SignalDefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            native_name="panel_area",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        # Simulate an author-customised binding with a nested path.
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_data_path="building.envelope.panel_area",
            is_required=False,
        )

        count = ensure_step_signal_bindings(step)

        self.assertEqual(count, 0)
        binding = StepSignalBinding.objects.get(
            workflow_step=step,
            signal_definition=sig,
        )
        # The custom path must be preserved, not overwritten.
        self.assertEqual(
            binding.source_data_path,
            "building.envelope.panel_area",
        )
        self.assertFalse(binding.is_required)

    def test_returns_count(self):
        """The return value should be the exact number of newly created
        bindings, which callers can use for logging or diagnostics.
        """
        validator = ValidatorFactory()
        sig1 = SignalDefinitionFactory(
            validator=validator,
            contract_key="sig_a",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.CATALOG,
        )
        SignalDefinitionFactory(
            validator=validator,
            contract_key="sig_b",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.CATALOG,
        )
        step = WorkflowStepFactory(validator=validator)

        # Pre-create one binding so only one should be new.
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig1,
        )

        count = ensure_step_signal_bindings(step)

        self.assertEqual(count, 1)

    def test_no_validator_noop(self):
        """Steps without a validator (e.g., action steps) should return 0
        without querying for signals or creating any bindings.

        We use a step with its validator_id cleared in memory (not saved)
        because the DB-level XOR constraint requires either a validator or
        an action to be set.
        """
        step = WorkflowStepFactory()
        # Simulate an action step by clearing the validator FK in memory.
        step.validator = None
        step.validator_id = None

        count = ensure_step_signal_bindings(step)

        self.assertEqual(count, 0)
