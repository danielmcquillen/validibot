"""
Tests for the Pydantic step config models in ``step_configs.py``.

These tests verify the ``EnergyPlusStepConfig`` schema definition used by
parameterized EnergyPlus templates. Template variable metadata is now stored
relationally in ``SignalDefinition`` rows (not in the step config), but the
config still carries template settings like ``case_sensitive`` and the
pre-existing simulation fields (``idf_checks``, ``run_simulation``, etc.).

The key invariant is backward compatibility — existing steps with no template
fields must parse without errors.
"""

from validibot.validations.constants import ENERGYPLUS_MODEL_TEMPLATE
from validibot.workflows.step_configs import EnergyPlusStepConfig

# ==============================================================================
# EnergyPlusStepConfig — template-related fields
# ==============================================================================


class TestEnergyPlusStepConfigTemplateFields:
    """Tests for the template-related fields on ``EnergyPlusStepConfig``.

    Template variable metadata is now stored relationally in
    ``SignalDefinition`` rows, but the config still carries
    ``case_sensitive``, ``display_signals``, and the pre-existing
    simulation fields.  These tests verify that:

    - Defaults are correct so existing steps parse without errors.
    - The ``extra="allow"`` behavior is preserved (runtime-injected keys
      like ``primary_file_uri`` must not cause validation errors).
    """

    def test_fields_have_correct_defaults(self):
        """Existing EnergyPlus steps have no template data in their config JSON.

        All fields must default to values that mean "no template active"
        so that ``get_step_config(existing_step)`` continues to work.
        """
        config = EnergyPlusStepConfig()

        assert config.case_sensitive is True
        assert config.display_signals == []

    def test_existing_fields_still_work(self):
        """Simulation fields (idf_checks, run_simulation, timestep_per_hour)
        are unchanged — this verifies we didn't accidentally break them.
        """
        config = EnergyPlusStepConfig(
            idf_checks=["duplicate-names"],
            run_simulation=True,
            timestep_per_hour=6,
        )

        assert config.idf_checks == ["duplicate-names"]
        assert config.run_simulation is True
        assert config.timestep_per_hour == 6  # noqa: PLR2004

    def test_backward_compat_no_template_fields(self):
        """Config JSON from a pre-template step must parse without errors.

        This is the most critical backward-compatibility test. Existing
        steps have config dicts like ``{"idf_checks": [], ...}``
        with no template keys. Pydantic must use defaults for missing fields.
        """
        legacy_config = {
            "idf_checks": ["duplicate-names"],
            "run_simulation": False,
            "timestep_per_hour": 4,
        }

        config = EnergyPlusStepConfig.model_validate(legacy_config)

        assert config.idf_checks == ["duplicate-names"]
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
