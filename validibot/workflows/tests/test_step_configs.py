"""
Tests for the two-bucket Pydantic step config models in ``step_configs.py``.

ADR-2026-06-18 split ``WorkflowStep.config`` into a **semantic** bucket
(``config``, hashed) and a **cosmetic** bucket (``display_settings``, never
hashed). These tests verify that split at the model layer:

- The semantic models (``EnergyPlusStepConfig`` etc.) carry only
  validation-affecting keys, and legacy configs still parse.
- The display models (``EnergyPlusDisplaySettings`` etc.) carry the cosmetic
  keys (``display_step_outputs``, ``show_energyplus_warnings``) and tolerate the
  runtime-injected keys the runner adds at launch (``primary_file_uri``).
- ``partition_step_config`` routes a freshly-built config dict into the two
  buckets using the semantic model's declared field set as the single
  discriminator.

The forbid-on-``config`` negative assertion lives with the flip in a later stage;
here the semantic models are still permissive during the staged migration.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from validibot.validations.constants import ENERGYPLUS_MODEL_TEMPLATE
from validibot.workflows.step_configs import EnergyPlusDisplaySettings
from validibot.workflows.step_configs import EnergyPlusStepConfig
from validibot.workflows.step_configs import TabularStepConfig
from validibot.workflows.step_configs import partition_step_config

# ==============================================================================
# EnergyPlusStepConfig — SEMANTIC bucket
# ==============================================================================


class TestEnergyPlusStepConfigSemanticFields:
    """The semantic model carries only validation-affecting knobs.

    Template variable metadata is stored relationally in ``StepIODefinition``
    rows; the config still carries the simulation/template knobs
    (``idf_checks``, ``run_simulation``, ``timestep_per_hour``,
    ``case_sensitive``, ``validation_mode``). Cosmetic keys moved out to the
    display model, so they must NOT be attributes here.
    """

    def test_fields_have_correct_defaults(self):
        """Existing EnergyPlus steps with no template data must parse cleanly.

        All semantic fields default to "no template active" so
        ``get_step_config(existing_step)`` keeps working after the split.
        """
        config = EnergyPlusStepConfig()

        assert config.case_sensitive is True
        assert config.validation_mode == ""
        assert config.idf_checks == []

    def test_existing_simulation_fields_still_work(self):
        """Simulation fields are unchanged by the split — a regression guard.

        These knobs decide what the validator runs, so they stay in the hashed
        semantic bucket.
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
        """A pre-template config dict must parse without errors.

        Existing steps have config dicts like ``{"idf_checks": [], ...}`` with no
        template keys; Pydantic must fall back to defaults for missing fields.
        """
        legacy_config = {
            "idf_checks": ["duplicate-names"],
            "run_simulation": False,
            "timestep_per_hour": 4,
        }

        config = EnergyPlusStepConfig.model_validate(legacy_config)

        assert config.idf_checks == ["duplicate-names"]
        assert config.case_sensitive is True

    def test_cosmetic_keys_are_not_semantic_fields(self):
        """``display_step_outputs`` / ``show_energyplus_warnings`` moved OUT.

        They belong to the display bucket now, so they must not be declared
        fields on the semantic model (otherwise the split leaks cosmetic data
        into the hashed bucket).
        """
        assert "display_step_outputs" not in EnergyPlusStepConfig.model_fields
        assert "show_energyplus_warnings" not in EnergyPlusStepConfig.model_fields

    def test_config_forbids_extra_keys(self):
        """The semantic bucket REJECTS undeclared keys — the crux of the split.

        This is what makes "hash ``config`` wholesale" correct by construction: a
        cosmetic key (``show_energyplus_warnings``) or a run-injected key
        (``primary_file_uri``) in ``config`` is a bug, so Pydantic must raise
        rather than silently absorb it into the hashed preimage. The permissive
        ``display_settings`` bucket is where such keys belong
        (see ``test_runtime_injected_keys_tolerated``).
        """
        with pytest.raises(PydanticValidationError):
            EnergyPlusStepConfig.model_validate(
                {"idf_checks": [], "show_energyplus_warnings": False},
            )
        with pytest.raises(PydanticValidationError):
            EnergyPlusStepConfig.model_validate(
                {"idf_checks": [], "primary_file_uri": "gs://bucket/model.idf"},
            )

    def test_case_sensitive_false(self):
        """Authors can opt into case-insensitive variable matching.

        When ``case_sensitive=False`` the scanner normalizes variable names to
        uppercase — a semantic change to what the template pipeline substitutes.
        """
        config = EnergyPlusStepConfig(case_sensitive=False)
        assert config.case_sensitive is False


# ==============================================================================
# EnergyPlusDisplaySettings — COSMETIC bucket
# ==============================================================================


class TestEnergyPlusDisplaySettings:
    """The display model carries cosmetic keys and tolerates injected keys."""

    def test_display_fields_have_correct_defaults(self):
        """Cosmetic defaults mean "show nothing extra" so old steps render.

        ``display_step_outputs`` is opt-in (empty = show none);
        ``show_energyplus_warnings`` defaults to True (show warnings).
        """
        display = EnergyPlusDisplaySettings()

        assert display.display_step_outputs == []
        assert display.show_energyplus_warnings is True

    def test_runtime_injected_keys_tolerated(self):
        """The display bucket must accept keys the runner injects at launch.

        ``extra="allow"`` on the display model is what keeps container launch
        (which merges keys like ``primary_file_uri``) from raising. This is the
        permanent home for such keys after the split.
        """
        display = EnergyPlusDisplaySettings.model_validate(
            {
                "display_step_outputs": ["total-energy"],
                "primary_file_uri": "file:///test/model.idf",
                "some_future_key": True,
            }
        )

        assert display.display_step_outputs == ["total-energy"]
        assert display.model_extra["primary_file_uri"] == "file:///test/model.idf"


# ==============================================================================
# partition_step_config — the split router
# ==============================================================================


class TestPartitionStepConfig:
    """Verify a merged config dict is routed into the two buckets correctly.

    The semantic model's declared field set is the single discriminator, so
    "what is hashed" and "what the model declares semantic" can never drift.
    """

    def test_tabular_split(self):
        """Tabular dialect keys stay semantic; labels/counts go to display.

        ``delimiter``/``encoding``/``has_header`` change how the file parses (so
        they must be hashed); ``delimiter_label``/``column_count``/preview are
        for the summary card only.
        """
        merged = {
            "delimiter": ",",
            "encoding": "utf-8",
            "has_header": True,
            "schema_source": "text",
            "schema_text_preview": "col_a,col_b",
            "delimiter_label": "Comma",
            "column_count": 2,
            "required_column_count": 1,
        }

        config, display = partition_step_config("TABULAR", merged)

        assert config == {"delimiter": ",", "encoding": "utf-8", "has_header": True}
        assert display == {
            "schema_source": "text",
            "schema_text_preview": "col_a,col_b",
            "delimiter_label": "Comma",
            "column_count": 2,
            "required_column_count": 1,
        }
        # The two buckets are disjoint — nothing is duplicated or dropped.
        assert set(config) | set(display) == set(merged)
        assert not (set(config) & set(display))

    def test_display_step_outputs_always_cosmetic(self):
        """``display_step_outputs`` is never a semantic field, for any type.

        It controls only what the submitter sees, so it must always route to the
        display bucket regardless of validator type.
        """
        _config, display = partition_step_config(
            "ENERGYPLUS",
            {"idf_checks": [], "display_step_outputs": ["x"]},
        )

        assert display == {"display_step_outputs": ["x"]}

    def test_undeclared_key_routes_to_display(self):
        """An undeclared key is fail-safe-routed to the permissive display bucket.

        This keeps a stray/new/runtime key out of the hashed semantic bucket even
        if a model wasn't updated — under-including in the hash is the safe error.
        """
        config, display = partition_step_config(
            "JSON_SCHEMA",
            {"schema_type": "2020-12", "primary_file_uri": "gs://b/x"},
        )

        assert config == {"schema_type": "2020-12"}
        assert display == {"primary_file_uri": "gs://b/x"}

    def test_unknown_type_treats_all_fields_as_display(self):
        """An unknown type must not guess which fields affect its semantics.

        Falling back to an empty ``BaseStepConfig`` keeps arbitrary keys out of
        the hashed bucket until the type registers an explicit config model.
        """
        config, display = partition_step_config(None, {"a": 1, "b": 2})

        assert config == {}
        assert display == {"a": 1, "b": 2}


# ==============================================================================
# ENERGYPLUS_MODEL_TEMPLATE constant
# ==============================================================================


class TestEnergyPlusModelTemplateConstant:
    """Tests for the ``ENERGYPLUS_MODEL_TEMPLATE`` constant.

    This constant is used as the ``resource_type`` value on
    ``WorkflowStepResource`` rows with ``role=MODEL_TEMPLATE``. It must be a
    specific string value that distinguishes template IDFs from other resource
    types (like weather files).
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
        template files live on ``WorkflowStepResource.step_resource_file``, not
        in the ``ValidatorResourceFile`` catalog.
        """
        from validibot.validations.constants import ResourceFileType

        resource_type_values = {choice.value for choice in ResourceFileType}
        assert ENERGYPLUS_MODEL_TEMPLATE not in resource_type_values


# Keep the TabularStepConfig import exercised so the semantic model is covered by
# a direct construction too (defaults must be parse-safe for legacy rows).
def test_tabular_semantic_defaults():
    """A Tabular semantic model defaults to auto-detect dialect.

    Empty ``delimiter`` means "sniff at read time"; ``has_header`` defaults True.
    """
    config = TabularStepConfig()
    assert config.delimiter == ""
    assert config.has_header is True
