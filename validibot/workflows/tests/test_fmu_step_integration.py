"""Tests for step-level FMU upload integration.

This test suite verifies the step-level FMU upload feature introduced by
ADR-2026-03-12. The feature allows workflow authors to upload an FMU file
directly in the step configuration form (instead of requiring a pre-built
library validator), mirroring the EnergyPlus parameterized template pattern.

Key areas covered:

1. **Shared introspection layer** — ``introspect_fmu()`` correctly parses
   variables and DefaultExperiment from modelDescription.xml, returning
   plain dataclasses that both the library and step-level flows consume.

2. **Step config building** — ``build_fmu_config()`` converts introspection
   results into step config dicts with ``fmu_simulation`` settings (variable
   metadata is stored relationally in ``SignalDefinition`` rows).

3. **Unified signals integration** —
   ``build_unified_signals_from_definitions()`` correctly treats FMU
   variables as a third signal source (``"fmu"``) alongside
   ``"catalog"`` and ``"template"``.

4. **Step config Pydantic models** — ``FmuStepConfig`` and
   ``FMUSimulationConfig`` correctly validate and serialize step config.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from django.test import TestCase

from validibot.validations.services.fmu import FMUIntrospectionError
from validibot.validations.services.fmu import FMUIntrospectionResult
from validibot.validations.services.fmu import FMUSimulationDefaults
from validibot.validations.services.fmu import FMUVariableInfo
from validibot.validations.services.fmu import introspect_fmu
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import StepSignalBindingFactory
from validibot.workflows.step_configs import FMUSimulationConfig
from validibot.workflows.step_configs import FmuStepConfig
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import build_unified_signals_from_definitions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _feedthrough_fmu_bytes() -> bytes:
    """Load the canned Feedthrough FMU from test assets."""
    asset = (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "assets"
        / "fmu"
        / "Feedthrough.fmu"
    )
    return asset.read_bytes()


def _make_minimal_fmu(
    model_name: str = "TestModel",
    variables_xml: str = "",
    default_experiment_xml: str = "",
) -> bytes:
    """Create a minimal FMU ZIP with a custom modelDescription.xml.

    This lets tests control exactly what variables and DefaultExperiment
    are present without needing a real compiled FMU.
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="{model_name}">
  <CoSimulation modelIdentifier="{model_name}" />
  {default_experiment_xml}
  <ModelVariables>
    {variables_xml}
  </ModelVariables>
</fmiModelDescription>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("modelDescription.xml", xml.encode())
    return buf.getvalue()


def _create_fmu_input_signal(step, *, contract_key, native_name, label="", **kwargs):
    """Create a step-owned FMU input SignalDefinition with a binding.

    Helper to reduce boilerplate in tests that set up FMU input signals.
    Returns the created SignalDefinition.
    """
    sig = SignalDefinitionFactory(
        workflow_step=step,
        validator=None,
        contract_key=contract_key,
        native_name=native_name,
        label=label,
        direction="input",
        origin_kind="fmu",
        **kwargs,
    )
    StepSignalBindingFactory(
        workflow_step=step,
        signal_definition=sig,
        source_data_path=native_name,
        is_required=True,
    )
    return sig


def _create_fmu_output_signal(step, *, contract_key, native_name, label="", **kwargs):
    """Create a step-owned FMU output SignalDefinition (no binding needed).

    Helper to reduce boilerplate in tests that set up FMU output signals.
    Returns the created SignalDefinition.
    """
    return SignalDefinitionFactory(
        workflow_step=step,
        validator=None,
        contract_key=contract_key,
        native_name=native_name,
        label=label,
        direction="output",
        origin_kind="fmu",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# introspect_fmu() — shared introspection layer
# ---------------------------------------------------------------------------


class IntrospectFmuTests(TestCase):
    """Tests for the shared ``introspect_fmu()`` function.

    These verify that the function correctly validates FMU archives,
    extracts variable metadata, and parses DefaultExperiment settings.
    Both the library-validator and step-level flows depend on this layer.
    """

    def test_valid_fmu_returns_result(self):
        """A valid FMU archive should produce an introspection result
        with model name, version, variables, and a checksum."""
        payload = _feedthrough_fmu_bytes()
        result = introspect_fmu(payload, "Feedthrough.fmu")

        self.assertIsInstance(result, FMUIntrospectionResult)
        self.assertEqual(result.model_name, "Feedthrough")
        self.assertEqual(result.fmi_version, "2.0")
        self.assertGreater(len(result.variables), 0)
        self.assertTrue(result.checksum)

    def test_variables_are_plain_dataclasses(self):
        """Variables should be FMUVariableInfo dataclasses, not Django models.
        This decoupling is what allows the step-level flow to store them
        as JSON dicts instead of database rows."""
        payload = _feedthrough_fmu_bytes()
        result = introspect_fmu(payload, "Feedthrough.fmu")

        for var in result.variables:
            self.assertIsInstance(var, FMUVariableInfo)
            self.assertTrue(var.name)
            self.assertTrue(var.causality)

    def test_input_and_output_variables_discovered(self):
        """The Feedthrough FMU has 4 input and 4 output variables.
        These should be correctly identified by causality."""
        payload = _feedthrough_fmu_bytes()
        result = introspect_fmu(payload, "Feedthrough.fmu")

        inputs = [v for v in result.variables if v.causality == "input"]
        outputs = [v for v in result.variables if v.causality == "output"]
        self.assertEqual(len(inputs), 4)
        self.assertEqual(len(outputs), 4)

    def test_default_experiment_parsed(self):
        """The Feedthrough FMU has ``<DefaultExperiment stopTime="2"/>``.
        This should be extracted into ``simulation_defaults``."""
        payload = _feedthrough_fmu_bytes()
        result = introspect_fmu(payload, "Feedthrough.fmu")

        self.assertIsInstance(result.simulation_defaults, FMUSimulationDefaults)
        self.assertEqual(result.simulation_defaults.stop_time, 2.0)

    def test_default_experiment_all_attributes(self):
        """When all four DefaultExperiment attributes are present, all
        should be parsed into the simulation defaults."""
        payload = _make_minimal_fmu(
            variables_xml="""
                <ScalarVariable name="x" valueReference="0" causality="input">
                  <Real start="0"/>
                </ScalarVariable>
            """,
            default_experiment_xml=(
                '<DefaultExperiment startTime="0" stopTime="3600"'
                ' stepSize="10" tolerance="1e-6"/>'
            ),
        )
        result = introspect_fmu(payload, "test.fmu")

        self.assertEqual(result.simulation_defaults.start_time, 0.0)
        self.assertEqual(result.simulation_defaults.stop_time, 3600.0)
        self.assertEqual(result.simulation_defaults.step_size, 10.0)
        self.assertEqual(result.simulation_defaults.tolerance, 1e-6)

    def test_no_default_experiment(self):
        """When the FMU has no DefaultExperiment element, all simulation
        defaults should be None."""
        payload = _make_minimal_fmu(
            variables_xml="""
                <ScalarVariable name="x" valueReference="0" causality="input">
                  <Real start="0"/>
                </ScalarVariable>
            """,
        )
        result = introspect_fmu(payload, "test.fmu")

        self.assertIsNone(result.simulation_defaults.start_time)
        self.assertIsNone(result.simulation_defaults.stop_time)
        self.assertIsNone(result.simulation_defaults.step_size)
        self.assertIsNone(result.simulation_defaults.tolerance)

    def test_variable_description_parsed(self):
        """The ``description`` attribute on ScalarVariable should be captured.
        This provides human-readable labels for the unified signals card."""
        payload = _make_minimal_fmu(
            variables_xml="""
                <ScalarVariable name="T_room" valueReference="0"
                    causality="output" description="Room temperature">
                  <Real/>
                </ScalarVariable>
            """,
        )
        result = introspect_fmu(payload, "test.fmu")

        self.assertEqual(len(result.variables), 1)
        self.assertEqual(result.variables[0].description, "Room temperature")

    # ── Error cases ──────────────────────────────────────────────

    def test_invalid_zip_raises_error(self):
        """A non-ZIP file should raise FMUIntrospectionError."""
        with pytest.raises(FMUIntrospectionError):
            introspect_fmu(b"not a zip file", "bad.fmu")

    def test_missing_model_description_raises_error(self):
        """A ZIP without modelDescription.xml should raise an error."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "hello")
        with pytest.raises(FMUIntrospectionError):
            introspect_fmu(buf.getvalue(), "missing.fmu")

    def test_oversized_payload_raises_error(self):
        """An FMU exceeding MAX_FMU_SIZE_BYTES should be rejected."""
        from validibot.validations.services.fmu import MAX_FMU_SIZE_BYTES

        big_payload = b"x" * (MAX_FMU_SIZE_BYTES + 1)
        with pytest.raises(FMUIntrospectionError):
            introspect_fmu(big_payload, "huge.fmu")


# ---------------------------------------------------------------------------
# Step config Pydantic models
# ---------------------------------------------------------------------------


class FmuStepConfigModelTests(TestCase):
    """Tests for the ``FmuStepConfig`` Pydantic model.

    Verifies that step config JSON with ``fmu_simulation`` is correctly
    parsed and validated.  FMU variable metadata is now stored
    relationally in ``SignalDefinition`` rows, not in the step config.
    """

    def test_empty_config_valid(self):
        """An empty config (library validator path) should be valid."""
        config = FmuStepConfig.model_validate({})
        self.assertIsNone(config.fmu_simulation)

    def test_simulation_config_roundtrip(self):
        """A config with simulation settings should roundtrip through
        Pydantic validation."""
        data = {
            "fmu_simulation": {
                "start_time": 0.0,
                "stop_time": 3600.0,
                "step_size": 10.0,
                "tolerance": 1e-6,
            },
        }
        config = FmuStepConfig.model_validate(data)

        self.assertIsNotNone(config.fmu_simulation)
        self.assertEqual(config.fmu_simulation.stop_time, 3600.0)

    def test_simulation_config_all_none(self):
        """FMUSimulationConfig with no values should have all None fields."""
        sim = FMUSimulationConfig()
        self.assertIsNone(sim.start_time)
        self.assertIsNone(sim.stop_time)
        self.assertIsNone(sim.step_size)
        self.assertIsNone(sim.tolerance)

    def test_extra_fields_allowed(self):
        """BaseStepConfig uses ``extra='allow'`` — runtime-injected keys
        (like ``primary_file_uri``) should not cause validation errors."""
        data = {
            "primary_file_uri": "gs://bucket/some.fmu",
        }
        FmuStepConfig.model_validate(data)  # Should not raise


# ---------------------------------------------------------------------------
# build_unified_signals_from_definitions() — FMU source type
#
# These tests use real database objects (WorkflowStep, SignalDefinition,
# StepSignalBinding) instead of the old _FakeStep mock. The function
# queries the DB for step-owned signals and their bindings.
# ---------------------------------------------------------------------------


class UnifiedSignalsFmuTests(TestCase):
    """Tests for FMU variable integration in
    ``build_unified_signals_from_definitions()``.

    Verifies that step-owned FMU ``SignalDefinition`` rows appear as
    input/output signals with source ``"fmu"``, alongside the existing
    ``"catalog"`` and ``"template"`` sources.
    """

    def test_fmu_input_variables_become_input_signals(self):
        """FMU variables with ``direction="input"`` should appear in
        the input signals list with ``source="fmu"``."""
        step = WorkflowStepFactory()
        _create_fmu_input_signal(
            step,
            contract_key="t_outdoor",
            native_name="T_outdoor",
            label="Outdoor",
        )
        _create_fmu_input_signal(
            step,
            contract_key="q_equipment",
            native_name="Q_equipment",
        )
        result = build_unified_signals_from_definitions(step=step)

        self.assertEqual(len(result["input_signals"]), 2)
        self.assertTrue(result["has_inputs"])

        sig = result["input_signals"][0]
        self.assertEqual(sig["slug"], "T_outdoor")
        self.assertEqual(sig["label"], "Outdoor")
        self.assertEqual(sig["source"], "fmu")
        self.assertTrue(sig["required"])

    def test_fmu_output_variables_become_output_signals(self):
        """FMU variables with ``direction="output"`` should appear in
        the output signals list with ``show_to_user=True`` by default."""
        step = WorkflowStepFactory()
        _create_fmu_output_signal(
            step,
            contract_key="t_room",
            native_name="T_room",
            label="Room temp",
        )
        _create_fmu_output_signal(
            step,
            contract_key="q_cool",
            native_name="Q_cool",
        )
        result = build_unified_signals_from_definitions(step=step)

        self.assertEqual(len(result["output_signals"]), 2)
        self.assertTrue(result["has_outputs"])

        sig = result["output_signals"][0]
        self.assertEqual(sig["slug"], "T_room")
        self.assertEqual(sig["label"], "Room temp")
        self.assertTrue(sig["show_to_user"])

    def test_parameter_variables_excluded(self):
        """FMU variables with causality other than input/output should not
        appear as either input or output signals — they are internal constants.

        In the new model, parameter variables simply aren't created as
        SignalDefinition rows with direction="input" or "output", so they
        naturally don't appear. This test confirms no leakage when only
        an input signal exists alongside other step data."""
        step = WorkflowStepFactory()
        # Only create the input signal — no parameter signal definition
        # would be created in the real flow (parameters are filtered out
        # during FMU introspection → SignalDefinition creation).
        _create_fmu_input_signal(
            step,
            contract_key="t_outdoor",
            native_name="T_outdoor",
        )
        result = build_unified_signals_from_definitions(step=step)

        self.assertEqual(len(result["input_signals"]), 1)
        self.assertEqual(result["input_signals"][0]["slug"], "T_outdoor")
        self.assertEqual(len(result["output_signals"]), 0)

    def test_display_signals_filter_fmu_outputs(self):
        """The ``display_signals`` config should control the ``show_to_user``
        flag on FMU output variables, just like it does for catalog outputs."""
        step = WorkflowStepFactory()
        _create_fmu_output_signal(
            step,
            contract_key="t_room",
            native_name="T_room",
        )
        _create_fmu_output_signal(
            step,
            contract_key="q_cool",
            native_name="Q_cool",
        )
        # display_signals uses contract_key for matching.
        step.config = {"display_signals": ["t_room"]}
        step.save()

        result = build_unified_signals_from_definitions(step=step)

        signals_by_slug = {s["slug"]: s for s in result["output_signals"]}
        self.assertTrue(signals_by_slug["T_room"]["show_to_user"])
        self.assertFalse(signals_by_slug["Q_cool"]["show_to_user"])

    def test_empty_fmu_variables_no_signals(self):
        """When no SignalDefinition rows exist for the step (library
        validator path with no signal definitions), no signals should appear."""
        step = WorkflowStepFactory()
        result = build_unified_signals_from_definitions(step=step)

        self.assertEqual(len(result["input_signals"]), 0)
        self.assertEqual(len(result["output_signals"]), 0)
        self.assertFalse(result["has_inputs"])
        self.assertFalse(result["has_outputs"])

    def test_label_fallback_chain(self):
        """Signal label should fall back: label → native_name → contract_key."""
        step = WorkflowStepFactory()
        _create_fmu_input_signal(
            step,
            contract_key="var1",
            native_name="var1",
            label="Custom Label",
        )
        _create_fmu_input_signal(
            step,
            contract_key="var2",
            native_name="var2_native",
            label="",
        )
        _create_fmu_input_signal(
            step,
            contract_key="var3",
            native_name="",
            label="",
        )
        result = build_unified_signals_from_definitions(step=step)

        labels = [s["label"] for s in result["input_signals"]]
        self.assertEqual(labels, ["Custom Label", "var2_native", "var3"])


# ---------------------------------------------------------------------------
# End-to-end: ServerRoomCooling FMU through the full pipeline
# ---------------------------------------------------------------------------


def _server_room_fmu_bytes() -> bytes:
    """Load the ServerRoomCooling FMU from test assets.

    This FMU is a real OpenModelica-compiled model with 4 inputs,
    2 outputs, 3 parameters, and fully populated DefaultExperiment.
    It exercises more of the introspection pipeline than Feedthrough.
    """
    asset = (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "assets"
        / "fmu"
        / "ServerRoomCooling.fmu"
    )
    return asset.read_bytes()


class ServerRoomCoolingEndToEndTests(TestCase):
    """End-to-end tests using the ServerRoomCooling FMU.

    These tests verify the full pipeline: introspect a real-world FMU,
    convert the result to step config, then feed through
    ``build_unified_signals_from_definitions()``. This catches integration issues that
    synthetic FMUs (``_make_minimal_fmu``) might miss — for example,
    OpenModelica emits ``causality="unknown"`` for internal/derivative
    variables, which must be filtered out of signals.
    """

    def test_introspection_finds_correct_variable_counts(self):
        """The ServerRoomCooling FMU has exactly 4 inputs, 2 outputs,
        and 3 parameters. Internal/derivative variables have
        ``causality='unknown'`` and must not be counted as
        inputs or outputs."""
        result = introspect_fmu(_server_room_fmu_bytes(), "ServerRoomCooling.fmu")

        inputs = [v for v in result.variables if v.causality == "input"]
        outputs = [v for v in result.variables if v.causality == "output"]
        params = [v for v in result.variables if v.causality == "parameter"]

        self.assertEqual(len(inputs), 4, "Expected 4 input variables")
        self.assertEqual(len(outputs), 2, "Expected 2 output variables")
        self.assertEqual(len(params), 3, "Expected 3 parameter variables")

    def test_introspection_simulation_defaults(self):
        """The ServerRoomCooling FMU has a fully populated DefaultExperiment:
        start=0, stop=3600, step=10, tolerance=1e-6."""
        result = introspect_fmu(_server_room_fmu_bytes(), "ServerRoomCooling.fmu")

        self.assertEqual(result.simulation_defaults.start_time, 0.0)
        self.assertEqual(result.simulation_defaults.stop_time, 3600.0)
        self.assertEqual(result.simulation_defaults.step_size, 10.0)
        self.assertEqual(result.simulation_defaults.tolerance, 1e-6)

    def test_introspection_variable_descriptions(self):
        """All input and output variables in the ServerRoomCooling FMU
        have descriptions. These become the default signal labels in
        the unified signals card (via the label fallback chain)."""
        result = introspect_fmu(_server_room_fmu_bytes(), "ServerRoomCooling.fmu")

        for var in result.variables:
            if var.causality in ("input", "output"):
                self.assertTrue(
                    var.description,
                    f"Variable {var.name} should have a description",
                )

    def test_introspection_to_step_config_roundtrip(self):
        """Introspection results should convert cleanly to step config
        dicts that ``FmuStepConfig`` can validate. Variable metadata is
        now stored in ``SignalDefinition`` rows, so only simulation
        settings go in the step config."""
        result = introspect_fmu(_server_room_fmu_bytes(), "ServerRoomCooling.fmu")

        config_data = {
            "fmu_simulation": {
                "start_time": result.simulation_defaults.start_time,
                "stop_time": result.simulation_defaults.stop_time,
                "step_size": result.simulation_defaults.step_size,
                "tolerance": result.simulation_defaults.tolerance,
            },
        }

        config = FmuStepConfig.model_validate(config_data)
        self.assertEqual(config.fmu_simulation.stop_time, 3600.0)

    def test_full_pipeline_introspection_to_signals(self):
        """Verify the complete pipeline: introspect → SignalDefinition rows →
        unified signals. The ServerRoomCooling FMU's 4 inputs should
        become 4 input signals and 2 outputs should become 2 output
        signals. Parameters and internal variables must be excluded."""
        result = introspect_fmu(_server_room_fmu_bytes(), "ServerRoomCooling.fmu")

        step = WorkflowStepFactory()

        # Create SignalDefinition + StepSignalBinding rows for each
        # input/output variable, mirroring the real FMU upload flow.
        for var in result.variables:
            if var.causality == "input":
                _create_fmu_input_signal(
                    step,
                    contract_key=var.name.lower(),
                    native_name=var.name,
                    label=var.description,
                )
            elif var.causality == "output":
                _create_fmu_output_signal(
                    step,
                    contract_key=var.name.lower(),
                    native_name=var.name,
                    label=var.description,
                )
            # Parameters and unknown causalities are intentionally skipped —
            # they are not created as SignalDefinition rows.

        signals = build_unified_signals_from_definitions(step=step)

        self.assertEqual(len(signals["input_signals"]), 4)
        self.assertEqual(len(signals["output_signals"]), 2)
        self.assertTrue(signals["has_inputs"])
        self.assertTrue(signals["has_outputs"])

        # Input signals should have source="fmu".
        for sig in signals["input_signals"]:
            self.assertEqual(sig["source"], "fmu")

        # All signals should use description as label (no explicit labels set).
        for sig in signals["input_signals"] + signals["output_signals"]:
            self.assertNotEqual(
                sig["label"],
                sig["slug"],
                "Label should be the description, not the variable name",
            )

    def test_input_variable_names(self):
        """Verify the specific input variable names from the
        ServerRoomCooling model match what the blog post expects."""
        result = introspect_fmu(_server_room_fmu_bytes(), "ServerRoomCooling.fmu")

        input_names = sorted(v.name for v in result.variables if v.causality == "input")
        self.assertEqual(
            input_names,
            [
                "Q_cooling_max",
                "Q_equipment",
                "T_outdoor",
                "T_setpoint",
            ],
        )
