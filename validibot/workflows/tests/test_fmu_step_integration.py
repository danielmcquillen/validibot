"""Tests for step-level FMU upload integration.

This test suite verifies the step-level FMU upload feature, which
allows workflow authors to upload an FMU file directly in the step
configuration form (instead of requiring a pre-built library
validator), mirroring the EnergyPlus parameterized template pattern.

Key areas covered:

1. **Shared introspection layer** — ``introspect_fmu()`` correctly parses
   variables and DefaultExperiment from modelDescription.xml, returning
   plain dataclasses that both the library and step-level flows consume.

2. **Step config building** — ``build_fmu_config()`` converts introspection
   results into step config dicts with ``fmu_simulation`` settings (variable
   metadata is stored relationally in ``StepIODefinition`` rows).

3. **Unified step I/O integration** —
   ``build_step_io_context()`` correctly treats FMU
   variables as a step-owned I/O source (``"fmu"``) alongside
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
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.workflows.step_configs import FMUSimulationConfig
from validibot.workflows.step_configs import FmuStepConfig
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import build_step_io_context

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


def _create_fmu_input_definition(
    step, *, contract_key, native_name, label="", **kwargs
):
    """Create a step-owned FMU input StepIODefinition with a binding.

    Helper to reduce boilerplate in tests that set up FMU step inputs.
    Returns the created StepIODefinition.
    """
    io_definition = StepIODefinitionFactory(
        workflow_step=step,
        validator=None,
        contract_key=contract_key,
        native_name=native_name,
        label=label,
        direction="input",
        origin_kind="fmu",
        **kwargs,
    )
    StepInputBindingFactory(
        workflow_step=step,
        io_definition=io_definition,
        source_data_path=native_name,
        is_required=True,
    )
    return io_definition


def _create_fmu_output_definition(
    step, *, contract_key, native_name, label="", **kwargs
):
    """Create a step-owned FMU output StepIODefinition (no binding needed).

    Helper to reduce boilerplate in tests that set up FMU output values.
    Returns the created StepIODefinition.
    """
    return StepIODefinitionFactory(
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

    def test_has_simulation_defaults_false_for_empty_default_experiment(self):
        """``has_simulation_defaults`` returns False for ``<DefaultExperiment/>``.

        Per the May 2026 P3 finding: the parser fact was originally
        named ``has_default_experiment``, which implied XML-element
        presence, but the underlying ``FMUSimulationDefaults`` dataclass
        only retains populated field values — element presence with no
        attributes is indistinguishable from absent. We renamed the
        fact to ``has_simulation_defaults`` so the name matches the
        observable: True iff at least one of startTime / stopTime /
        stepSize / tolerance was set.

        This test pins the contract: an empty ``<DefaultExperiment/>``
        with no timing attributes returns False, matching the renamed
        semantic. A regression that flipped this would silently let
        ``i.has_simulation_defaults`` resolve True for an FMU that
        shipped no usable defaults.
        """
        from validibot.validations.services.fmu import build_introspection_metadata

        payload = _make_minimal_fmu(
            variables_xml="""
                <ScalarVariable name="x" valueReference="0" causality="input">
                  <Real start="0"/>
                </ScalarVariable>
            """,
            default_experiment_xml="<DefaultExperiment />",
        )
        result = introspect_fmu(payload, "empty_default.fmu")
        metadata = build_introspection_metadata(result)

        self.assertFalse(metadata["has_simulation_defaults"])

    def test_has_simulation_defaults_true_with_any_timing_field(self):
        """Any populated timing field flips ``has_simulation_defaults`` True.

        Companion to the empty-DefaultExperiment test: just one
        populated attribute (here, stopTime) is enough. The "any one
        is enough" semantic is what the rename
        (``has_default_experiment`` → ``has_simulation_defaults``)
        was meant to clarify.
        """
        from validibot.validations.services.fmu import build_introspection_metadata

        payload = _make_minimal_fmu(
            variables_xml="""
                <ScalarVariable name="x" valueReference="0" causality="input">
                  <Real start="0"/>
                </ScalarVariable>
            """,
            default_experiment_xml='<DefaultExperiment stopTime="86400.0"/>',
        )
        result = introspect_fmu(payload, "partial_default.fmu")
        metadata = build_introspection_metadata(result)

        self.assertTrue(metadata["has_simulation_defaults"])

    def test_variable_description_parsed(self):
        """The ``description`` attribute on ScalarVariable should be captured.
        This provides human-readable labels for the Inputs and Outputs card."""
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
# Tri-state build_fmu_config + _sync_fmu_step_io (May 2026 P1 fix)
# ---------------------------------------------------------------------------


class BuildFmuConfigTriStateTests(TestCase):
    """``build_fmu_config`` returns the right tri-state for I/O definition sync.

    The tri-state (``None`` / ``[]`` / ``[...]``) was introduced after
    the May 2026 review caught that editing a step's simulation
    timing without re-uploading the FMU was clearing every step-owned
    FMU I/O definition — including parser facts and any author-built
    StepInputBindings — even though the FMU resource was untouched.

    These tests pin the contract:
      - No new upload, no removal → ``None`` (no-op, preserve rows)
      - User clicked remove          → ``[]``  (clear all FMU I/O definitions)
      - New upload                   → ``[var dicts]`` (sync variables)
    """

    def _make_form(self, **overrides):
        """Build a lightweight form stub with the fields build_fmu_config reads.

        Avoids instantiating the full crispy form, which would require
        validator + step setup we don't need for a unit test of the
        config-building logic.
        """
        from types import SimpleNamespace

        cleaned_data = {
            "fmu_file": None,
            "remove_fmu": False,
            "sim_start_time": None,
            "sim_stop_time": None,
            "sim_step_size": None,
            "sim_tolerance": None,
        }
        cleaned_data.update(overrides)
        return SimpleNamespace(
            cleaned_data=cleaned_data,
            is_system_validator=True,
        )

    def _make_step_with_existing_fmu(self):
        """Create a workflow step that has an existing FMU upload baked in.

        We seed StepIODefinitions and step.config the way build_fmu_config
        + _sync_fmu_step_io would have on a prior upload. This lets
        subsequent calls observe whether they preserve or destroy that
        existing state.
        """
        from validibot.validations.services.fmu_step_io import (
            sync_step_fmu_io_definitions,
        )

        step = WorkflowStepFactory()
        sync_step_fmu_io_definitions(
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
        step.config = {
            "fmu_simulation": {
                "start_time": 0.0,
                "stop_time": 86400.0,
                "step_size": 60.0,
                "tolerance": None,
            },
            "fmu_introspection": {
                "model_name": "Original",
                "fmi_version": "2.0",
                "variable_count": 1,
                "input_variable_count": 1,
                "output_variable_count": 0,
                "parameter_count": 0,
                "has_simulation_defaults": True,
            },
        }
        step.save()
        return step

    def test_no_upload_no_removal_preserves_step_io(self):
        """Editing simulation timing without re-uploading returns fmu_vars=None.

        This is the May 2026 P1 fix in action: when the author tweaks
        ``sim_stop_time`` and saves, the resulting tri-state must NOT
        be ``[]`` (which would clear all I/O definitions); it must be ``None``
        so ``_sync_fmu_step_io`` takes the no-op branch.
        """
        from validibot.workflows.views_helpers import build_fmu_config

        step = self._make_step_with_existing_fmu()
        form = self._make_form(sim_stop_time=172800.0)  # changed timing

        config, fmu_vars = build_fmu_config(form, step)

        self.assertIsNone(
            fmu_vars,
            "No new upload AND no removal MUST return None — "
            "returning [] would cascade clear_step_fmu_io_definitions.",
        )
        # The config still gets rebuilt from form overrides, so the
        # timing change must land in fmu_simulation. fmu_introspection
        # must survive too (it came from the prior upload and is still
        # valid).
        self.assertEqual(config["fmu_simulation"]["stop_time"], 172800.0)
        self.assertIn("fmu_introspection", config)
        self.assertEqual(config["fmu_introspection"]["model_name"], "Original")

    def test_remove_fmu_returns_empty_list_for_step_io_sync(self):
        """Clicking "remove FMU" returns fmu_vars=[] so I/O definitions are cleared.

        The empty list is the deliberate instruction to _sync_fmu_step_io
        that the FMU is gone — distinct from None (no change). The
        config is also cleared in this branch.
        """
        from validibot.workflows.views_helpers import build_fmu_config

        step = self._make_step_with_existing_fmu()
        form = self._make_form(remove_fmu=True)

        config, fmu_vars = build_fmu_config(form, step)

        self.assertEqual(fmu_vars, [])
        self.assertEqual(config, {})

    def test_sync_fmu_step_io_none_preserves_step_io_definition_pks(self):
        """End-to-end: tri-state None preserves existing StepIODefinition PKs.

        Combines the build_fmu_config tri-state with _sync_fmu_step_io'
        no-op branch to prove the full flow. Snapshots PKs before and
        after; any regression to the old ``fmu_vars=[]`` contract
        would destroy them all.
        """
        from validibot.validations.models import StepIODefinition
        from validibot.workflows.views_helpers import _sync_fmu_step_io
        from validibot.workflows.views_helpers import build_fmu_config

        step = self._make_step_with_existing_fmu()
        pre_pks = set(
            StepIODefinition.objects.filter(workflow_step=step).values_list(
                "pk",
                flat=True,
            ),
        )
        self.assertGreater(len(pre_pks), 0)  # sanity

        # Edit simulation timing without uploading anything new.
        form = self._make_form(sim_step_size=120.0)
        _config, fmu_vars = build_fmu_config(form, step)
        _sync_fmu_step_io(step, fmu_vars)

        post_pks = set(
            StepIODefinition.objects.filter(workflow_step=step).values_list(
                "pk",
                flat=True,
            ),
        )
        self.assertEqual(
            pre_pks,
            post_pks,
            "StepIODefinition rows must survive a config-only edit "
            "with no FMU re-upload — regression to the May 2026 P1 bug.",
        )


class FmuStepConfigModelTests(TestCase):
    """Tests for the ``FmuStepConfig`` Pydantic model.

    Verifies that step config JSON with ``fmu_simulation`` is correctly
    parsed and validated.  FMU variable metadata is now stored
    relationally in ``StepIODefinition`` rows, not in the step config.
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

    def test_extra_fields_forbidden_in_config_allowed_in_display(self):
        """The SEMANTIC FmuStepConfig FORBIDS undeclared keys (ADR-2026-06-18).

        This is what lets the workflow-definition digest hash ``config``
        wholesale: a run-injected key like ``primary_file_uri`` must not be
        absorbed into the hashed bucket. Such keys belong in the display bucket
        (``BaseDisplaySettings``), which still uses ``extra="allow"``.
        """
        from pydantic import ValidationError as PydanticValidationError

        from validibot.workflows.step_configs import BaseDisplaySettings

        with pytest.raises(PydanticValidationError):
            FmuStepConfig.model_validate({"primary_file_uri": "gs://bucket/some.fmu"})

        # The display bucket is the correct home for runtime-injected keys.
        display = BaseDisplaySettings.model_validate(
            {"primary_file_uri": "gs://bucket/some.fmu"},
        )
        self.assertEqual(
            display.model_extra["primary_file_uri"],
            "gs://bucket/some.fmu",
        )


# ---------------------------------------------------------------------------
# build_step_io_context() — FMU source type
#
# These tests use real database objects (WorkflowStep, StepIODefinition,
# StepInputBinding) instead of the old _FakeStep mock. The function
# queries the DB for step-owned I/O definitions and their bindings.
# ---------------------------------------------------------------------------


class UnifiedStepIOFMUTests(TestCase):
    """Tests for FMU variable integration in
    ``build_step_io_context()``.

    Verifies that step-owned FMU ``StepIODefinition`` rows appear as
    input/output values with source ``"fmu"``, alongside the existing
    ``"catalog"`` and ``"template"`` sources.
    """

    def test_fmu_input_variables_become_input_values(self):
        """FMU variables with ``direction="input"`` should appear in
        the step inputs list with ``source="fmu"``."""
        step = WorkflowStepFactory()
        _create_fmu_input_definition(
            step,
            contract_key="t_outdoor",
            native_name="T_outdoor",
            label="Outdoor",
        )
        _create_fmu_input_definition(
            step,
            contract_key="q_equipment",
            native_name="Q_equipment",
        )
        result = build_step_io_context(step=step)

        self.assertEqual(len(result["input_values"]), 2)
        self.assertTrue(result["has_inputs"])

        io_definition = result["input_values"][0]
        self.assertEqual(io_definition["slug"], "t_outdoor")
        self.assertEqual(io_definition["label"], "Outdoor")
        self.assertEqual(io_definition["source"], "fmu")
        self.assertTrue(io_definition["required"])

    def test_fmu_output_variables_become_output_values(self):
        """FMU variables with ``direction="output"`` should appear in
        the output values list without being displayed by default."""
        step = WorkflowStepFactory()
        _create_fmu_output_definition(
            step,
            contract_key="t_room",
            native_name="T_room",
            label="Room temp",
        )
        _create_fmu_output_definition(
            step,
            contract_key="q_cool",
            native_name="Q_cool",
        )
        result = build_step_io_context(step=step)

        self.assertEqual(len(result["output_values"]), 2)
        self.assertTrue(result["has_outputs"])

        io_definition = result["output_values"][0]
        self.assertEqual(io_definition["slug"], "t_room")
        self.assertEqual(io_definition["label"], "Room temp")
        self.assertFalse(io_definition["show_to_user"])

    def test_parameter_variables_excluded(self):
        """FMU variables with causality other than input/output should not
        appear as either input or output values — they are internal constants.

        In the new model, parameter variables simply aren't created as
        StepIODefinition rows with direction="input" or "output", so they
        naturally don't appear. This test confirms no leakage when only
        an step input exists alongside other step data."""
        step = WorkflowStepFactory()
        # Only create the step input — no parameter step I/O definition
        # would be created in the real flow (parameters are filtered out
        # during FMU introspection → StepIODefinition creation).
        _create_fmu_input_definition(
            step,
            contract_key="t_outdoor",
            native_name="T_outdoor",
        )
        result = build_step_io_context(step=step)

        self.assertEqual(len(result["input_values"]), 1)
        self.assertEqual(result["input_values"][0]["slug"], "t_outdoor")
        self.assertEqual(len(result["output_values"]), 0)

    def test_display_step_outputs_filter_fmu_outputs(self):
        """The ``display_step_outputs`` config should control the ``show_to_user``
        flag on FMU output variables, just like it does for catalog outputs."""
        step = WorkflowStepFactory()
        _create_fmu_output_definition(
            step,
            contract_key="t_room",
            native_name="T_room",
        )
        _create_fmu_output_definition(
            step,
            contract_key="q_cool",
            native_name="Q_cool",
        )
        # display_step_outputs uses contract_key for matching. It is cosmetic, so
        # it lives in the display bucket now (ADR-2026-06-18).
        step.display_settings = {"display_step_outputs": ["t_room"]}
        step.save()

        result = build_step_io_context(step=step)

        outputs_by_slug = {s["slug"]: s for s in result["output_values"]}
        self.assertTrue(outputs_by_slug["t_room"]["show_to_user"])
        self.assertFalse(outputs_by_slug["q_cool"]["show_to_user"])

    def test_empty_fmu_variables_have_no_step_io(self):
        """When no StepIODefinition rows exist for the step (library
        validator path with no step I/O definitions), no inputs or outputs appear."""
        step = WorkflowStepFactory()
        result = build_step_io_context(step=step)

        self.assertEqual(len(result["input_values"]), 0)
        self.assertEqual(len(result["output_values"]), 0)
        self.assertFalse(result["has_inputs"])
        self.assertFalse(result["has_outputs"])

    def test_label_fallback_chain(self):
        """Step I/O label should fall back: label → native_name → contract_key."""
        step = WorkflowStepFactory()
        _create_fmu_input_definition(
            step,
            contract_key="var1",
            native_name="var1",
            label="Custom Label",
        )
        _create_fmu_input_definition(
            step,
            contract_key="var2",
            native_name="var2_native",
            label="",
        )
        _create_fmu_input_definition(
            step,
            contract_key="var3",
            native_name="",
            label="",
        )
        result = build_step_io_context(step=step)

        labels = [s["label"] for s in result["input_values"]]
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
    ``build_step_io_context()``. This catches integration issues that
    synthetic FMUs (``_make_minimal_fmu``) might miss — for example,
    OpenModelica emits ``causality="unknown"`` for internal/derivative
    variables, which must be filtered out of the step I/O contract.
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
        have descriptions. These become the default labels in the unified
        Inputs and Outputs card (via the label fallback chain)."""
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
        now stored in ``StepIODefinition`` rows, so only simulation
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

    def test_full_pipeline_introspection_to_step_io(self):
        """Verify the complete pipeline: introspect → StepIODefinition rows →
        the unified step I/O context. The ServerRoomCooling FMU's 4 inputs
        should become 4 step inputs and its 2 outputs should become 2 step
        outputs. Parameters and internal variables must be excluded."""
        result = introspect_fmu(_server_room_fmu_bytes(), "ServerRoomCooling.fmu")

        step = WorkflowStepFactory()

        # Create StepIODefinition + StepInputBinding rows for each
        # input/output variable, mirroring the real FMU upload flow.
        for var in result.variables:
            if var.causality == "input":
                _create_fmu_input_definition(
                    step,
                    contract_key=var.name.lower(),
                    native_name=var.name,
                    label=var.description,
                )
            elif var.causality == "output":
                _create_fmu_output_definition(
                    step,
                    contract_key=var.name.lower(),
                    native_name=var.name,
                    label=var.description,
                )
            # Parameters and unknown causalities are intentionally skipped —
            # they are not created as StepIODefinition rows.

        step_io = build_step_io_context(step=step)

        self.assertEqual(len(step_io["input_values"]), 4)
        self.assertEqual(len(step_io["output_values"]), 2)
        self.assertTrue(step_io["has_inputs"])
        self.assertTrue(step_io["has_outputs"])

        # Step inputs should have source="fmu".
        for io_definition in step_io["input_values"]:
            self.assertEqual(io_definition["source"], "fmu")

        # All rows should use description as label (no explicit labels set).
        for io_definition in step_io["input_values"] + step_io["output_values"]:
            self.assertNotEqual(
                io_definition["label"],
                io_definition["slug"],
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
