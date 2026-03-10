"""
Tests for the Pydantic step config models in ``step_configs.py``.

These tests verify the schema definitions that drive parameterized EnergyPlus
templates. The Pydantic models define how template variable metadata is
structured, validated, and stored in ``WorkflowStep.config``. Getting the
schema right is critical because:

- ``TemplateVariable`` enforces a constrained ``Literal`` type on
  ``variable_type`` — invalid values must be rejected at parse time, not
  downstream in the substitution pipeline.
- ``EnergyPlusStepConfig`` must be backward-compatible — existing steps with
  no template fields must parse without errors.
- The ``IDFTemplateVariable`` subclass establishes a type boundary that
  future template formats (epJSON, gbXML) will use for format-specific
  dispatch.

Phase: 1 (Data Model and Config) of the EnergyPlus Parameterized Templates
ADR.
"""

import pytest
from pydantic import ValidationError

from validibot.validations.constants import ENERGYPLUS_MODEL_TEMPLATE
from validibot.workflows.step_configs import EnergyPlusStepConfig
from validibot.workflows.step_configs import IDFTemplateVariable
from validibot.workflows.step_configs import TemplateVariable

# ==============================================================================
# TemplateVariable — base schema for template placeholders
# ==============================================================================


class TestTemplateVariable:
    """Tests for the base ``TemplateVariable`` Pydantic model.

    ``TemplateVariable`` is the generic schema for template placeholders.
    It defines name, description, defaults, type constraints, and allowed
    values. These tests verify that defaults are correct, all fields accept
    their expected types, and invalid ``variable_type`` values are rejected.
    """

    def test_minimal_construction(self):
        """Only ``name`` is required — all other fields have sensible defaults.

        This matters because the IDF scanner creates ``TemplateVariable``
        instances with just a name initially, then the author fills in
        descriptions, defaults, and constraints later.
        """
        var = TemplateVariable(name="U_FACTOR")

        assert var.name == "U_FACTOR"
        assert var.description == ""
        assert var.default == ""
        assert var.units == ""
        assert var.variable_type == "text"
        assert var.min_value is None
        assert var.min_exclusive is False
        assert var.max_value is None
        assert var.max_exclusive is False
        assert var.choices == []

    def test_full_construction(self):
        """All fields can be populated — e.g., a fully annotated number variable.

        A window U-factor has a numeric range, display units, and a sensible
        default. This verifies the full schema round-trips correctly.
        """
        var = TemplateVariable(
            name="U_FACTOR",
            description="Window U-Factor",
            default="2.0",
            units="W/m2-K",
            variable_type="number",
            min_value=0.1,
            min_exclusive=False,
            max_value=10.0,
            max_exclusive=True,
            choices=[],
        )

        assert var.name == "U_FACTOR"
        assert var.description == "Window U-Factor"
        assert var.default == "2.0"
        assert var.units == "W/m2-K"
        assert var.variable_type == "number"
        assert var.min_value == pytest.approx(0.1)
        assert var.max_value == pytest.approx(10.0)
        assert var.max_exclusive is True

    def test_choice_variable(self):
        """Choice-type variables restrict submitter input to a predefined list.

        This is common for EnergyPlus fields that accept enumerated values
        like surface roughness.
        """
        var = TemplateVariable(
            name="ROUGHNESS",
            description="Surface Roughness",
            variable_type="choice",
            choices=[
                "VeryRough",
                "Rough",
                "MediumRough",
                "MediumSmooth",
                "Smooth",
                "VerySmooth",
            ],
        )

        assert var.variable_type == "choice"
        assert len(var.choices) == 6  # noqa: PLR2004
        assert "VerySmooth" in var.choices

    def test_invalid_variable_type_rejected(self):
        """``variable_type`` is a Literal — invalid values must fail validation.

        If Pydantic silently accepted ``'integer'`` or ``'float'``, the
        downstream merge/validate logic would get an unexpected type and
        potentially skip validation entirely.
        """
        with pytest.raises(ValidationError, match="variable_type"):
            TemplateVariable(name="BAD", variable_type="integer")

    def test_variable_type_literal_values(self):
        """All three valid ``variable_type`` values must be accepted.

        This guards against accidentally narrowing the Literal in a future
        refactor.
        """
        for vtype in ("text", "number", "choice"):
            var = TemplateVariable(name="TEST", variable_type=vtype)
            assert var.variable_type == vtype

    def test_serialization_round_trip(self):
        """A ``TemplateVariable`` must survive dict → model → dict round-trip.

        Template variables are stored in ``WorkflowStep.config`` (a JSONField),
        so they must serialize to plain dicts and deserialize back identically.
        """
        data = {
            "name": "SHGC",
            "description": "Solar Heat Gain Coefficient",
            "default": "0.4",
            "units": "",
            "variable_type": "number",
            "min_value": 0.0,
            "min_exclusive": True,
            "max_value": 1.0,
            "max_exclusive": False,
            "choices": [],
        }
        var = TemplateVariable.model_validate(data)
        output = var.model_dump()

        assert output["name"] == "SHGC"
        assert output["min_exclusive"] is True
        assert output["max_value"] == pytest.approx(1.0)


# ==============================================================================
# IDFTemplateVariable — IDF-specific subclass
# ==============================================================================


class TestIDFTemplateVariable:
    """Tests for the ``IDFTemplateVariable`` subclass.

    ``IDFTemplateVariable`` currently adds no extra fields — it exists to
    establish a type boundary for IDF-specific validation rules (structural
    character blocking, Autosize/Autocalculate acceptance). These tests
    verify that it correctly inherits all parent fields and that
    ``EnergyPlusStepConfig.template_variables`` accepts it.
    """

    def test_inherits_all_fields(self):
        """``IDFTemplateVariable`` must expose every ``TemplateVariable`` field.

        If a field is accidentally shadowed or the inheritance chain breaks,
        downstream code that reads ``var.min_value`` on an IDF variable would
        get an ``AttributeError``.
        """
        var = IDFTemplateVariable(
            name="U_FACTOR",
            description="U-Factor",
            default="2.0",
            units="W/m2-K",
            variable_type="number",
            min_value=0.1,
            max_value=10.0,
        )

        assert var.name == "U_FACTOR"
        assert var.units == "W/m2-K"
        assert var.variable_type == "number"
        assert var.min_value == pytest.approx(0.1)

    def test_is_subclass_of_template_variable(self):
        """Type hierarchy must be correct for isinstance checks.

        Code that accepts ``TemplateVariable`` must also accept
        ``IDFTemplateVariable``. This is the whole point of the subclass
        pattern — generic code works on the base, IDF-specific code narrows.
        """
        var = IDFTemplateVariable(name="SHGC")
        assert isinstance(var, TemplateVariable)

    def test_rejects_invalid_variable_type(self):
        """Literal constraint is inherited — invalid types rejected on subclass too."""
        with pytest.raises(ValidationError, match="variable_type"):
            IDFTemplateVariable(name="BAD", variable_type="boolean")


# ==============================================================================
# EnergyPlusStepConfig — template-related fields
# ==============================================================================


class TestEnergyPlusStepConfigTemplateFields:
    """Tests for the new template-related fields on ``EnergyPlusStepConfig``.

    Phase 1 adds ``template_variables``, ``case_sensitive``, and
    ``display_signals`` to ``EnergyPlusStepConfig``. These tests verify that:

    - Defaults are correct (empty list, True, empty list) so existing steps
      without template data parse without errors.
    - Template variable data round-trips through the config model.
    - The ``extra="allow"`` behavior is preserved (runtime-injected keys
      like ``primary_file_uri`` must not cause validation errors).
    """

    def test_new_fields_have_correct_defaults(self):
        """Existing EnergyPlus steps have no template data in their config JSON.

        All new fields must default to values that mean "no template active"
        so that ``get_step_config(existing_step)`` continues to work.
        """
        config = EnergyPlusStepConfig()

        assert config.template_variables == []
        assert config.case_sensitive is True
        assert config.display_signals == []

    def test_existing_fields_still_work(self):
        """Phase 0 fields (idf_checks, run_simulation, timestep_per_hour) are
        unchanged — this verifies we didn't accidentally break them.
        """
        config = EnergyPlusStepConfig(
            idf_checks=["duplicate-names"],
            run_simulation=True,
            timestep_per_hour=6,
        )

        assert config.idf_checks == ["duplicate-names"]
        assert config.run_simulation is True
        assert config.timestep_per_hour == 6  # noqa: PLR2004

    def test_template_variables_round_trip(self):
        """Template variables stored as dicts in JSON must parse into typed
        ``IDFTemplateVariable`` instances and serialize back to dicts.

        This is the core serialization contract: ``WorkflowStep.config``
        stores plain JSON, and ``EnergyPlusStepConfig`` must reconstruct
        typed variable objects from that JSON.
        """
        config_data = {
            "idf_checks": [],
            "run_simulation": True,
            "template_variables": [
                {
                    "name": "U_FACTOR",
                    "description": "Window U-Factor",
                    "default": "2.0",
                    "units": "W/m2-K",
                    "variable_type": "number",
                    "min_value": 0.1,
                    "max_value": 10.0,
                },
                {
                    "name": "SHGC",
                    "description": "Solar Heat Gain Coefficient",
                    "variable_type": "number",
                    "min_value": 0.0,
                    "min_exclusive": True,
                    "max_value": 1.0,
                },
            ],
            "case_sensitive": True,
            "display_signals": ["total-site-energy", "heating-energy"],
        }

        config = EnergyPlusStepConfig.model_validate(config_data)

        # Variables parsed into typed objects
        assert len(config.template_variables) == 2  # noqa: PLR2004
        assert isinstance(config.template_variables[0], IDFTemplateVariable)
        assert config.template_variables[0].name == "U_FACTOR"
        assert config.template_variables[0].units == "W/m2-K"
        assert config.template_variables[1].min_exclusive is True

        # Display signals preserved
        assert config.display_signals == ["total-site-energy", "heating-energy"]

        # Round-trip back to dict
        dumped = config.model_dump()
        assert len(dumped["template_variables"]) == 2  # noqa: PLR2004
        assert dumped["template_variables"][0]["name"] == "U_FACTOR"
        assert dumped["case_sensitive"] is True

    def test_backward_compat_no_template_fields(self):
        """Config JSON from a pre-template step (no template_variables key)
        must parse without errors.

        This is the most critical backward-compatibility test. Thousands of
        existing steps have config dicts like ``{"idf_checks": [], ...}``
        with no template keys. Pydantic must use defaults for missing fields.
        """
        legacy_config = {
            "idf_checks": ["duplicate-names"],
            "run_simulation": False,
            "timestep_per_hour": 4,
        }

        config = EnergyPlusStepConfig.model_validate(legacy_config)

        assert config.idf_checks == ["duplicate-names"]
        assert config.template_variables == []
        assert config.case_sensitive is True
        assert config.display_signals == []

    def test_extra_keys_allowed(self):
        """``extra="allow"`` must be preserved so runtime-injected keys
        (like ``primary_file_uri``) don't cause validation errors.

        This is inherited from ``BaseStepConfig`` and is essential for the
        container launch pipeline, which injects keys into the config dict
        at runtime.
        """
        config = EnergyPlusStepConfig.model_validate(
            {
                "idf_checks": [],
                "primary_file_uri": "file:///test/model.idf",
                "some_future_key": True,
            }
        )

        assert config.idf_checks == []
        # Extra keys don't cause errors — they're accessible but not typed
        assert config.model_extra["primary_file_uri"] == "file:///test/model.idf"

    def test_case_sensitive_false(self):
        """Authors can opt into case-insensitive variable matching.

        When ``case_sensitive=False``, the scanner normalizes all variable
        names to uppercase. This test verifies the field accepts False.
        """
        config = EnergyPlusStepConfig(case_sensitive=False)
        assert config.case_sensitive is False


# ==============================================================================
# ENERGYPLUS_MODEL_TEMPLATE constant
# ==============================================================================


class TestEnergyPlusModelTemplateConstant:
    """Tests for the ``ENERGYPLUS_MODEL_TEMPLATE`` constant.

    This constant is used as the ``resource_type`` value on
    ``WorkflowStepResource`` rows with ``role=MODEL_TEMPLATE``. It must be
    a specific string value that distinguishes template IDFs from other
    resource types (like weather files).
    """

    def test_constant_value(self):
        """The constant must have the expected string value.

        This is a snapshot test — if someone renames the constant's value,
        existing database rows with the old value would become orphaned.
        """
        assert ENERGYPLUS_MODEL_TEMPLATE == "energyplus_model_template"

    def test_constant_is_not_resource_file_type(self):
        """Template IDFs are step-owned, not catalog resources.

        The constant must NOT be a member of ``ResourceFileType`` because
        template files live on ``WorkflowStepResource.step_resource_file``,
        not in the ``ValidatorResourceFile`` catalog.
        """
        from validibot.validations.constants import ResourceFileType

        resource_type_values = {choice.value for choice in ResourceFileType}
        assert ENERGYPLUS_MODEL_TEMPLATE not in resource_type_values
