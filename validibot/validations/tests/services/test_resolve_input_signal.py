"""
Tests for the input signal resolution engine.

The resolution engine is the bridge between ``StepSignalBinding`` (which
declares *where* to find a signal value) and the actual data extraction.
It replaces the legacy approach of passing the entire submission JSON as
flat FMU input values, enabling:

- Nested path resolution (e.g., ``building.envelope.panel_area``)
- Default value fallback for optional signals
- Structured errors for missing required signals
- Audit tracing via ``ResolvedInputTrace`` rows

The core function ``resolve_input_signal()`` resolves a single binding;
``resolve_step_input_signals()`` batch-resolves all bindings for a step.
"""

from __future__ import annotations

import pytest
from django.test import TestCase

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import SignalDirection
from validibot.validations.models import ResolvedInputTrace
from validibot.validations.services.path_resolution import InputSignalResolutionError
from validibot.validations.services.path_resolution import resolve_input_signal
from validibot.validations.services.path_resolution import resolve_step_input_signals
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import StepSignalBindingFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


class ResolveInputSignalTests(TestCase):
    """Tests for the single-signal resolve_input_signal() function."""

    def _make_binding(self, *, step=None, scope=None, path="", **kwargs):
        """Create a signal definition and binding for testing."""
        step = step or WorkflowStepFactory()
        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
        )
        return StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=scope or BindingSourceScope.SUBMISSION_PAYLOAD,
            source_data_path=path,
            **kwargs,
        )

    def test_resolve_simple_top_level_key(self):
        """A flat key like 'T_outdoor' should resolve from top-level
        submission dict — the common case for FMU start_values.
        """
        binding = self._make_binding(path="T_outdoor")
        result = resolve_input_signal(
            binding,
            submission_data={"T_outdoor": 295.0},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 295.0)
        self.assertFalse(result.used_default)

    def test_resolve_nested_dotted_path(self):
        """Dotted paths like 'building.floor_area' should traverse nested
        dicts — enabling structured submission payloads.
        """
        binding = self._make_binding(path="building.floor_area")
        result = resolve_input_signal(
            binding,
            submission_data={"building": {"floor_area": 150.0}},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 150.0)

    def test_resolve_with_bracket_index(self):
        """Bracket notation should resolve array elements — needed for
        submissions with list structures.
        """
        binding = self._make_binding(path="zones[0].temp")
        result = resolve_input_signal(
            binding,
            submission_data={"zones": [{"temp": 22.5}]},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 22.5)

    def test_required_signal_missing_returns_error_result(self):
        """When a required signal's path doesn't match anything in the
        submission and no default is configured, resolve_input_signal
        returns an error result (resolved=False, error_message populated)
        instead of raising immediately. The batch resolver collects all
        errors and raises after building audit traces.
        """
        binding = self._make_binding(path="nonexistent", is_required=True)
        result = resolve_input_signal(binding, submission_data={"other": 1})
        self.assertFalse(result.resolved)
        self.assertIn(
            binding.signal_definition.contract_key,
            result.error_message,
        )

    def test_batch_raises_after_collecting_all_errors(self):
        """The batch resolver must collect ALL resolution errors and build
        ALL audit traces before raising InputSignalResolutionError. This
        ensures operators get complete diagnostic information — which
        signals resolved, which failed — not just the first failure.
        """
        step = WorkflowStepFactory()
        run = ValidationRunFactory(workflow=step.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
        )

        # Two required input signals, both missing from submission
        for name in ("signal_a", "signal_b"):
            sig = SignalDefinitionFactory(
                workflow_step=step,
                validator=None,
                direction=SignalDirection.INPUT,
                contract_key=name,
                native_name=name,
            )
            StepSignalBindingFactory(
                workflow_step=step,
                signal_definition=sig,
                source_data_path=name,
                is_required=True,
            )

        with pytest.raises(InputSignalResolutionError) as exc_info:
            resolve_step_input_signals(
                step,
                step_run,
                submission_data={},
            )

        # Both missing signals should be mentioned in the error message
        assert "signal_a" in str(exc_info.value)
        assert "signal_b" in str(exc_info.value)

        # Traces for BOTH signals should be attached to the exception
        assert len(exc_info.value.traces) == 2  # noqa: PLR2004

    def test_optional_signal_missing_returns_unresolved(self):
        """Optional signals (is_required=False) with no matching path and
        no default should return a ResolvedSignal with resolved=False and
        value=None — not raise an error.
        """
        binding = self._make_binding(
            path="nonexistent",
            is_required=False,
            default_value=None,
        )
        result = resolve_input_signal(binding, submission_data={"other": 1})
        self.assertFalse(result.resolved)
        self.assertIsNone(result.value)

    def test_default_value_used_when_path_not_found(self):
        """When the source path doesn't resolve but a default_value is
        configured, the resolution should succeed with used_default=True.
        This enables optional FMU parameters with sensible defaults.
        """
        binding = self._make_binding(
            path="missing_key",
            is_required=False,
            default_value=20.0,
        )
        result = resolve_input_signal(binding, submission_data={})
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 20.0)
        self.assertTrue(result.used_default)

    def test_submission_metadata_scope(self):
        """SUBMISSION_METADATA scope should resolve from metadata dict,
        not the payload — used for signals sourced from submission
        metadata fields (project name, upload date, etc.).
        """
        binding = self._make_binding(
            scope=BindingSourceScope.SUBMISSION_METADATA,
            path="project.floor_area",
        )
        result = resolve_input_signal(
            binding,
            submission_metadata={"project": {"floor_area": 200.0}},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 200.0)

    # ── Blank-path fallback to contract_key ──────────────────────────
    #
    # When source_data_path is empty, the resolver should use
    # contract_key as a top-level key in the scoped data, NOT return
    # the entire scoped dict.

    def test_blank_path_falls_back_to_contract_key(self):
        """When source_data_path is empty (''), the resolver should
        look up the signal's contract_key as a top-level key in the
        scoped data. This is the documented fallback: 'When empty,
        falls back to matching by contract_key as a top-level key in
        the scoped data.'
        """
        step = WorkflowStepFactory()
        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="T_outdoor",
        )
        binding = StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
            source_data_path="",  # blank — should fall back to contract_key
        )
        result = resolve_input_signal(
            binding,
            submission_data={"T_outdoor": 295.0, "other": 999},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 295.0)
        # Must NOT return the entire dict
        self.assertNotIsInstance(result.value, dict)

    def test_blank_path_not_found_uses_default(self):
        """When source_data_path is empty and contract_key is not in the
        scoped data, the fallback to default_value should still work.
        """
        step = WorkflowStepFactory()
        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="missing_signal",
        )
        binding = StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
            source_data_path="",
            default_value=42.0,
            is_required=False,
        )
        result = resolve_input_signal(
            binding,
            submission_data={"other": 1},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 42.0)
        self.assertTrue(result.used_default)

    # ── Upstream step output resolution ──────────────────────────────
    #
    # Upstream outputs are stored at run.summary["steps"][step_key]["output"].
    # The path format is "step_key.output_name" — the resolver flattens
    # the intermediate "output" key so this path works naturally.

    def test_upstream_step_signal_resolution(self):
        """Upstream step outputs should resolve via dotted path
        'step_key.output_name' against the flattened upstream dict.
        The raw upstream shape is {step_key: {"output": {...}}},
        and the resolver flattens away the intermediate 'output' key.
        """
        binding = self._make_binding(
            scope=BindingSourceScope.UPSTREAM_STEP,
            path="simulation.site_eui",
        )
        upstream = {
            "simulation": {
                "output": {
                    "site_eui": 85.3,
                    "source_eui": 120.0,
                },
            },
        }
        result = resolve_input_signal(
            binding,
            upstream_signals=upstream,
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 85.3)

    def test_upstream_step_sets_audit_step_key(self):
        """When resolving from UPSTREAM_STEP scope, the resolver should
        populate upstream_step_key on the result so audit traces record
        which upstream step was consulted. The step_key is the first
        segment of the dotted path (e.g., 'simulation' from
        'simulation.site_eui').
        """
        binding = self._make_binding(
            scope=BindingSourceScope.UPSTREAM_STEP,
            path="simulation.site_eui",
        )
        upstream = {
            "simulation": {
                "output": {"site_eui": 85.3},
            },
        }
        result = resolve_input_signal(
            binding,
            upstream_signals=upstream,
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.upstream_step_key, "simulation")

    def test_upstream_step_missing_signal_returns_error(self):
        """When an upstream step exists but the requested output name
        is not in its output dict, the resolver should return an
        unresolved result with an error message.
        """
        binding = self._make_binding(
            scope=BindingSourceScope.UPSTREAM_STEP,
            path="simulation.nonexistent",
            is_required=True,
        )
        upstream = {
            "simulation": {
                "output": {"site_eui": 85.3},
            },
        }
        result = resolve_input_signal(
            binding,
            upstream_signals=upstream,
        )
        self.assertFalse(result.resolved)
        self.assertIn(
            binding.signal_definition.contract_key,
            result.error_message,
        )

    # ── Metadata-backed EnergyPlus input end-to-end ──────────────────
    #
    # EnergyPlus validators declare some inputs as sourced from
    # submission.metadata (e.g., expected_floor_area_m2). The resolver
    # must correctly handle SUBMISSION_METADATA scope with a flat key.

    def test_metadata_flat_key_energyplus_style(self):
        """EnergyPlus metadata-backed inputs use SUBMISSION_METADATA scope
        with a flat key like 'floor_area_m2'. This simulates the end-to-end
        path from ensure_step_signal_bindings through resolution.
        """
        binding = self._make_binding(
            scope=BindingSourceScope.SUBMISSION_METADATA,
            path="floor_area_m2",
        )
        result = resolve_input_signal(
            binding,
            submission_metadata={"floor_area_m2": 250.0},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 250.0)


class ResolveStepInputSignalsTests(TestCase):
    """Tests for the batch resolve_step_input_signals() function."""

    def test_batch_returns_native_name_keyed_dict(self):
        """The batch resolver should return a dict keyed by native_name
        (the FMU variable name), not contract_key — because FMU runners
        expect start_values keyed by the actual variable name.
        """
        step = WorkflowStepFactory()
        run = ValidationRunFactory(workflow=step.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
        )

        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="t_outdoor",
            native_name="T_outdoor",
        )
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
            source_data_path="T_outdoor",
        )

        input_values, traces = resolve_step_input_signals(
            step,
            step_run,
            submission_data={"T_outdoor": 295.0},
        )

        self.assertEqual(input_values, {"T_outdoor": 295.0})
        self.assertEqual(len(traces), 1)

    def test_batch_creates_trace_instances(self):
        """The batch resolver should return ResolvedInputTrace instances
        (unsaved) that the caller can bulk_create for audit purposes.
        """
        step = WorkflowStepFactory()
        run = ValidationRunFactory(workflow=step.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
        )

        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="pressure",
            native_name="P_atm",
        )
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
            source_data_path="P_atm",
        )

        _, traces = resolve_step_input_signals(
            step,
            step_run,
            submission_data={"P_atm": 101325.0},
        )

        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertTrue(trace.resolved)
        self.assertEqual(trace.signal_contract_key, "pressure")
        self.assertEqual(trace.value_snapshot, 101325.0)

        # Verify they can be bulk_created
        ResolvedInputTrace.objects.bulk_create(traces)
        self.assertEqual(ResolvedInputTrace.objects.count(), 1)

    def test_batch_resolves_metadata_scoped_signals(self):
        """The batch resolver should pass submission_metadata through to
        individual signal resolution — critical for EnergyPlus inputs that
        source values from submission metadata (e.g., expected_floor_area_m2).
        """
        step = WorkflowStepFactory()
        run = ValidationRunFactory(workflow=step.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
        )

        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="floor_area_m2",
            native_name="expected_floor_area_m2",
        )
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SUBMISSION_METADATA,
            source_data_path="floor_area_m2",
        )

        input_values, traces = resolve_step_input_signals(
            step,
            step_run,
            submission_data={},
            submission_metadata={"floor_area_m2": 250.0},
        )

        self.assertEqual(input_values, {"expected_floor_area_m2": 250.0})
        self.assertEqual(len(traces), 1)
        self.assertTrue(traces[0].resolved)

    def test_batch_resolves_upstream_step_signals(self):
        """The batch resolver should correctly resolve signals scoped to
        UPSTREAM_STEP, navigating the 'step_key.signal_name' path against
        the flattened upstream signals dict.
        """
        step = WorkflowStepFactory()
        run = ValidationRunFactory(workflow=step.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
        )

        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="upstream_eui",
            native_name="target_eui",
        )
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.UPSTREAM_STEP,
            source_data_path="simulation.site_eui",
        )

        upstream = {
            "simulation": {
                "output": {"site_eui": 85.3},
            },
        }

        input_values, traces = resolve_step_input_signals(
            step,
            step_run,
            submission_data={},
            upstream_signals=upstream,
        )

        self.assertEqual(input_values, {"target_eui": 85.3})
        self.assertEqual(len(traces), 1)
        self.assertTrue(traces[0].resolved)

    def test_batch_blank_path_uses_contract_key(self):
        """The batch resolver should use contract_key as the lookup
        key when source_data_path is empty — matching the documented
        blank-path fallback contract.
        """
        step = WorkflowStepFactory()
        run = ValidationRunFactory(workflow=step.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
        )

        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="T_outdoor",
            native_name="T_outdoor",
        )
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
            source_data_path="",  # blank — should fall back to contract_key
        )

        input_values, traces = resolve_step_input_signals(
            step,
            step_run,
            submission_data={"T_outdoor": 295.0},
        )

        self.assertEqual(input_values, {"T_outdoor": 295.0})
        self.assertEqual(len(traces), 1)
        self.assertTrue(traces[0].resolved)


# ── SIGNAL scope: workflow signal resolution ────────────────────────
#
# The SIGNAL scope allows validator inputs to reference workflow-level
# signals (the ``s.`` namespace) resolved by ``resolve_workflow_signals()``.
# This bridges the two signal layers: a workflow signal mapping extracts
# a value from the submission, and a SIGNAL-scoped binding feeds it
# into a validator input.


class ResolveSignalScopeTests(TestCase):
    """Tests for the SIGNAL scope in resolve_input_signal().

    The SIGNAL scope lets validator inputs reference resolved workflow
    signals instead of raw submission data. This is the fix for the bug
    where ``source_data_path='s.solar_irradiance'`` failed because the
    resolver had no SIGNAL scope — it tried to look up the path in the
    submission payload, which doesn't have an ``s`` key.
    """

    def _make_binding(self, *, step=None, path="", **kwargs):
        """Create a SIGNAL-scoped binding for testing."""
        step = step or WorkflowStepFactory()
        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
        )
        return StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SIGNAL,
            source_data_path=path,
            **kwargs,
        )

    def test_resolves_from_workflow_signals_dict(self):
        """A SIGNAL-scoped binding with path 'solar_irradiance' should
        resolve from the workflow_signals dict, not the submission payload.
        """
        binding = self._make_binding(path="solar_irradiance")
        result = resolve_input_signal(
            binding,
            submission_data={"unrelated": 999},
            workflow_signals={"solar_irradiance": 800.0},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 800.0)

    def test_strips_s_dot_prefix(self):
        """Users may paste CEL references like 's.solar_irradiance' as the
        source path. The resolver should strip the 's.' prefix so the
        lookup matches the workflow_signals dict keys.
        """
        binding = self._make_binding(path="s.solar_irradiance")
        result = resolve_input_signal(
            binding,
            workflow_signals={"solar_irradiance": 800.0},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 800.0)
        self.assertEqual(result.source_data_path_used, "solar_irradiance")

    def test_missing_required_signal_returns_error(self):
        """When a required SIGNAL-scoped binding references a workflow
        signal that doesn't exist, the resolver should return an error
        result (not raise) so the batch resolver can collect all errors.
        """
        binding = self._make_binding(
            path="nonexistent_signal",
            is_required=True,
        )
        result = resolve_input_signal(
            binding,
            workflow_signals={"other_signal": 42},
        )
        self.assertFalse(result.resolved)
        self.assertIn(
            binding.signal_definition.contract_key,
            result.error_message,
        )

    def test_falls_back_to_default_value(self):
        """When a SIGNAL-scoped binding can't find the signal but has a
        default value configured, the resolver should use the default.
        """
        binding = self._make_binding(
            path="missing_signal",
            default_value=500.0,
            is_required=False,
        )
        result = resolve_input_signal(
            binding,
            workflow_signals={},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 500.0)
        self.assertTrue(result.used_default)

    def test_empty_workflow_signals_falls_back_to_default(self):
        """When workflow_signals is None (e.g., because workflow signal
        resolution failed), a binding with a default value should still
        resolve successfully.
        """
        binding = self._make_binding(
            path="solar_irradiance",
            default_value=600.0,
            is_required=False,
        )
        result = resolve_input_signal(
            binding,
            workflow_signals=None,
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 600.0)
        self.assertTrue(result.used_default)

    def test_blank_path_falls_back_to_contract_key(self):
        """When source_data_path is empty, the SIGNAL scope should fall
        back to looking up by contract_key, matching the blank-path
        fallback behavior of other scopes.
        """
        step = WorkflowStepFactory()
        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="solar_irradiance",
        )
        binding = StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SIGNAL,
            source_data_path="",
        )
        result = resolve_input_signal(
            binding,
            workflow_signals={"solar_irradiance": 800.0},
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.value, 800.0)


class ResolveStepSignalScopeBatchTests(TestCase):
    """Tests for SIGNAL scope in the batch resolve_step_input_signals().

    Verifies that workflow_signals are passed through correctly when
    resolving multiple bindings in a single batch call.
    """

    def test_batch_resolves_signal_scoped_bindings(self):
        """The batch resolver should pass workflow_signals through to
        individual binding resolution — enabling SIGNAL-scoped bindings
        to find workflow-level signal values.
        """
        step = WorkflowStepFactory()
        run = ValidationRunFactory(workflow=step.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
        )

        sig = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="solar_irradiance",
            native_name="GHI_W_m2",
        )
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig,
            source_scope=BindingSourceScope.SIGNAL,
            source_data_path="solar_irradiance",
        )

        input_values, traces = resolve_step_input_signals(
            step,
            step_run,
            submission_data={},
            workflow_signals={"solar_irradiance": 800.0},
        )

        self.assertEqual(input_values, {"GHI_W_m2": 800.0})
        self.assertEqual(len(traces), 1)
        self.assertTrue(traces[0].resolved)

    def test_batch_mixed_scopes(self):
        """A step can have bindings in different scopes — some from
        submission payload, some from workflow signals. The batch resolver
        should resolve each binding in its own scope correctly.
        """
        step = WorkflowStepFactory()
        run = ValidationRunFactory(workflow=step.workflow)
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
        )

        # Payload-scoped binding
        sig_payload = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="temperature",
            native_name="T_outdoor",
        )
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig_payload,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
            source_data_path="T_outdoor",
        )

        # Signal-scoped binding
        sig_signal = SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.INPUT,
            contract_key="irradiance",
            native_name="GHI_W_m2",
        )
        StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=sig_signal,
            source_scope=BindingSourceScope.SIGNAL,
            source_data_path="solar_irradiance",
        )

        input_values, traces = resolve_step_input_signals(
            step,
            step_run,
            submission_data={"T_outdoor": 295.0},
            workflow_signals={"solar_irradiance": 800.0},
        )

        self.assertEqual(
            input_values,
            {"T_outdoor": 295.0, "GHI_W_m2": 800.0},
        )
        self.assertEqual(len(traces), 2)
        self.assertTrue(all(t.resolved for t in traces))
