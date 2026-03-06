"""
Tests for the output signal display helper (Phase 6).

This module verifies that ``build_display_signals()`` and
``build_template_params_display()`` correctly:

- Extract raw signals from ``step_run.output["signals"]``
- Filter signals by the author's ``display_signals`` selection (or show all
  when the list is empty)
- Enrich each signal with catalog entry metadata (labels, units, precision)
- Format numeric values with thousands separators and configurable decimal
  precision
- Handle edge cases (no signals, missing catalog, unknown types)
- Build template parameter display data enriched with variable metadata

The signal display system is a **cross-validator capability** — any
validator type that populates ``step_run.output["signals"]`` gets signal
display automatically.  These tests verify that invariant by exercising
both EnergyPlus and non-EnergyPlus validators.
"""

import pytest

from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ValidationType
from validibot.validations.services.signal_display import _format_signal_value
from validibot.validations.services.signal_display import build_display_signals
from validibot.validations.services.signal_display import build_template_params_display
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

# ── Fixtures ─────────────────────────────────────────────────────────
# These fixtures build the model graph needed by the signal display
# helpers: Validator → WorkflowStep → ValidationStepRun, with optional
# catalog entries.  EnergyPlus validators get EnergyPlusStepConfig
# (which has the ``display_signals`` field); other validators use the
# generic BaseStepConfig (which does not).


def _make_energyplus_step_run(
    output: dict | None = None,
    *,
    display_signals: list[str] | None = None,
    template_variables: list[dict] | None = None,
):
    """Create a ValidationStepRun backed by an EnergyPlus step.

    Args:
        output: The ``step_run.output`` JSONField value.
        display_signals: Author-selected signal slugs.  When None,
            defaults to empty list (show all signals).
        template_variables: Optional list of template variable dicts
            for the step config.

    Returns:
        A saved ``ValidationStepRun`` instance.
    """
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    config: dict = {}
    if display_signals is not None:
        config["display_signals"] = display_signals
    if template_variables is not None:
        config["template_variables"] = template_variables
    step = WorkflowStepFactory(validator=validator, config=config)
    return ValidationStepRunFactory(
        workflow_step=step,
        output=output or {},
    )


def _make_fmu_step_run(output: dict | None = None):
    """Create a ValidationStepRun backed by an FMU validator.

    FMU step configs do NOT have a ``display_signals`` field, so this
    verifies the cross-validator fallback behavior (show all signals).
    """
    validator = ValidatorFactory(validation_type=ValidationType.FMU)
    step = WorkflowStepFactory(validator=validator, config={})
    return ValidationStepRunFactory(
        workflow_step=step,
        output=output or {},
    )


# ── build_display_signals ────────────────────────────────────────────
# Tests verifying the core signal enrichment function.


@pytest.mark.django_db
class TestBuildDisplaySignals:
    """Tests for ``build_display_signals()`` — signal enrichment."""

    def test_returns_empty_for_step_without_signals(self):
        """Step runs with no ``output["signals"]`` should return an
        empty list.  This is the common case for non-simulation
        validators like JSON Schema."""
        sr = _make_energyplus_step_run(output={})
        assert build_display_signals(sr) == []

    def test_no_signals_key_in_output(self):
        """If ``step_run.output`` exists but has no ``signals`` key,
        returns an empty list rather than raising a KeyError."""
        sr = _make_energyplus_step_run(output={"some_other_key": 42})
        assert build_display_signals(sr) == []

    def test_returns_all_signals_when_display_signals_empty(self):
        """When ``display_signals`` is ``[]`` (the default), all signals
        should be shown.  This is the backward-compatible behavior for
        validators that haven't been configured with signal selection."""
        sr = _make_energyplus_step_run(
            output={
                "signals": {
                    "electricity_kwh": 100.0,
                    "gas_kwh": 50.0,
                },
            },
            display_signals=[],
        )
        result = build_display_signals(sr)
        slugs = [s.slug for s in result]
        assert "electricity_kwh" in slugs
        assert "gas_kwh" in slugs

    def test_filters_by_display_signals(self):
        """When ``display_signals`` contains specific slugs, only those
        appear in the result.  The author's selection controls what
        submitters see, even when the runner extracts more signals."""
        sr = _make_energyplus_step_run(
            output={
                "signals": {
                    "electricity_kwh": 100.0,
                    "gas_kwh": 50.0,
                    "eui_kwh_m2": 89.1,
                },
            },
            display_signals=["electricity_kwh", "eui_kwh_m2"],
        )
        result = build_display_signals(sr)
        slugs = [s.slug for s in result]
        assert slugs == ["electricity_kwh", "eui_kwh_m2"]

    def test_enriches_with_catalog_metadata(self):
        """Each signal should get its label, units, and description from
        the matching ``ValidatorCatalogEntry``.  This test creates a
        catalog entry with specific metadata and verifies enrichment."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        ValidatorCatalogEntryFactory(
            validator=validator,
            slug="electricity_kwh",
            label="Site Electricity",
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            data_type=CatalogValueType.NUMBER,
            description="Total site electricity consumption.",
            metadata={"units": "kWh", "precision": 1},
            order=10,
        )
        step = WorkflowStepFactory(validator=validator, config={})
        sr = ValidationStepRunFactory(
            workflow_step=step,
            output={"signals": {"electricity_kwh": 12345.6}},
        )
        result = build_display_signals(sr)
        assert len(result) == 1
        signal = result[0]
        assert signal.label == "Site Electricity"
        assert signal.units == "kWh"
        assert signal.description == "Total site electricity consumption."
        assert signal.formatted_value == "12,345.6"  # precision=1

    def test_formats_float_with_default_precision(self):
        """Float values should be formatted with 2 decimal places and
        thousands separators when no precision is specified in catalog
        metadata."""
        sr = _make_energyplus_step_run(
            output={"signals": {"value": 12345.6}},
        )
        result = build_display_signals(sr)
        assert result[0].formatted_value == "12,345.60"

    def test_formats_integer_with_thousands_separator(self):
        """Integer values should get thousands separators and no decimal
        places (e.g., 12345 → '12,345')."""
        sr = _make_energyplus_step_run(
            output={"signals": {"count": 12345}},
        )
        result = build_display_signals(sr)
        assert result[0].formatted_value == "12,345"

    def test_formats_none_as_na(self):
        """``None`` signal values should be formatted as 'N/A' rather
        than displaying Python's ``None`` string."""
        sr = _make_energyplus_step_run(
            output={"signals": {"missing_value": None}},
        )
        result = build_display_signals(sr)
        assert result[0].formatted_value == "N/A"

    def test_formats_string_as_passthrough(self):
        """String signal values should pass through unchanged, without
        any numeric formatting applied."""
        sr = _make_energyplus_step_run(
            output={"signals": {"status": "Autosize"}},
        )
        result = build_display_signals(sr)
        assert result[0].formatted_value == "Autosize"

    def test_orders_by_catalog_order(self):
        """Signals should be sorted by their catalog entry's ``order``
        field, not by their slug or insertion order in the output dict.
        This lets authors control the display order via catalog config."""
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        ValidatorCatalogEntryFactory(
            validator=validator,
            slug="beta",
            order=20,
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
        )
        ValidatorCatalogEntryFactory(
            validator=validator,
            slug="alpha",
            order=10,
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
        )
        step = WorkflowStepFactory(validator=validator, config={})
        sr = ValidationStepRunFactory(
            workflow_step=step,
            output={"signals": {"beta": 2.0, "alpha": 1.0}},
        )
        result = build_display_signals(sr)
        assert [s.slug for s in result] == ["alpha", "beta"]

    def test_falls_back_to_slug_label_when_no_catalog(self):
        """When a signal slug has no matching catalog entry, the label
        should be derived from the slug itself by replacing underscores
        with spaces and title-casing."""
        sr = _make_energyplus_step_run(
            output={"signals": {"site_electricity_kwh": 100.0}},
        )
        result = build_display_signals(sr)
        assert result[0].label == "Site Electricity Kwh"

    def test_cross_validator_no_display_signals_attribute(self):
        """For validators whose step config model has no
        ``display_signals`` field (e.g., FMU), all signals should be
        shown.  This verifies the cross-validator generality using
        ``getattr(typed_config, 'display_signals', [])``."""
        sr = _make_fmu_step_run(
            output={
                "signals": {
                    "metric_a": 1.0,
                    "metric_b": 2.0,
                },
            },
        )
        result = build_display_signals(sr)
        slugs = [s.slug for s in result]
        assert "metric_a" in slugs
        assert "metric_b" in slugs

    def test_display_signals_with_nonexistent_slug(self):
        """If ``display_signals`` references a slug not present in the
        actual ``output["signals"]``, that slug is silently skipped.
        This handles the case where the author configured signals before
        any run produced them."""
        sr = _make_energyplus_step_run(
            output={"signals": {"electricity_kwh": 100.0}},
            display_signals=["electricity_kwh", "nonexistent_slug"],
        )
        result = build_display_signals(sr)
        slugs = [s.slug for s in result]
        assert slugs == ["electricity_kwh"]


# ── build_template_params_display ────────────────────────────────────
# Tests for the template parameter enrichment function (template-mode
# only).  Template parameters are submitted by the user and stored in
# step_run.output["template_parameters_used"] by the launcher.  The
# display function enriches them with labels/units from the step
# config's template_variables.


@pytest.mark.django_db
class TestBuildTemplateParamsDisplay:
    """Tests for ``build_template_params_display()``."""

    def test_returns_empty_for_non_template_step(self):
        """Step runs without ``template_parameters_used`` in their
        output should return an empty list.  This is the normal case
        for direct-mode (non-template) EnergyPlus runs."""
        sr = _make_energyplus_step_run(output={})
        assert build_template_params_display(sr) == []

    def test_returns_params_with_metadata(self):
        """Parameters should be enriched with labels and units from the
        step config's ``template_variables``.  The ``description`` field
        on a template variable serves as the human-readable label.

        The launcher stores parameters WITHOUT the ``$`` prefix — keys
        are plain variable names like ``"U_FACTOR"``, not ``"$U_FACTOR"``.
        The display helper must match on plain names.
        """
        sr = _make_energyplus_step_run(
            output={
                "template_parameters_used": {
                    "U_FACTOR": "2.0",
                    "COOLING_SETPOINT": "24.0",
                },
            },
            template_variables=[
                {
                    "name": "U_FACTOR",
                    "description": "Window U-Factor",
                    "units": "W/m2-K",
                    "variable_type": "number",
                },
                {
                    "name": "COOLING_SETPOINT",
                    "description": "Cooling Setpoint Temperature",
                    "units": "°C",
                    "variable_type": "number",
                },
            ],
        )
        result = build_template_params_display(sr)
        expected_count = 2
        assert len(result) == expected_count

        u_factor = next(p for p in result if p["name"] == "U_FACTOR")
        assert u_factor["label"] == "Window U-Factor"
        assert u_factor["value"] == "2.0"
        assert u_factor["units"] == "W/m2-K"

        setpoint = next(p for p in result if p["name"] == "COOLING_SETPOINT")
        assert setpoint["label"] == "Cooling Setpoint Temperature"
        assert setpoint["units"] == "°C"

    def test_falls_back_to_variable_name_as_label(self):
        """When ``template_variables`` has no description for a variable,
        the raw variable name is used as the label.  This handles runs
        that occurred before the author annotated variables."""
        sr = _make_energyplus_step_run(
            output={
                "template_parameters_used": {"UNKNOWN_VAR": "42"},
            },
            template_variables=[],  # No variable metadata
        )
        result = build_template_params_display(sr)
        assert len(result) == 1
        assert result[0]["label"] == "UNKNOWN_VAR"
        assert result[0]["value"] == "42"


# ── _format_signal_value ─────────────────────────────────────────────
# Tests for the numeric formatting helper.  This function handles the
# various types that signal values can take (None, int, float, str,
# bool) and applies precision and thousands-separator formatting.


class TestFormatSignalValue:
    """Tests for ``_format_signal_value()`` — numeric formatting."""

    def test_none_returns_na(self):
        """``None`` → ``'N/A'`` — signals without a value should show
        a human-readable placeholder."""
        assert _format_signal_value(None) == "N/A"

    def test_zero_float(self):
        """``0.0`` → ``'0.00'`` — zero should still be formatted with
        the default 2 decimal places."""
        assert _format_signal_value(0.0) == "0.00"

    def test_negative_float(self):
        """``-1234.5`` → ``'-1,234.50'`` — negative values should keep
        their sign and get thousands separators."""
        assert _format_signal_value(-1234.5) == "-1,234.50"

    def test_very_large_number(self):
        """Large numbers should get thousands separators for
        readability (e.g., ``1000000.0`` → ``'1,000,000.00'``)."""
        assert _format_signal_value(1000000.0) == "1,000,000.00"

    def test_precision_zero(self):
        """When the catalog specifies ``precision=0``, the value should
        be rounded to the nearest integer and displayed without decimal
        places (e.g., ``12345.678`` → ``'12,346'``)."""
        assert _format_signal_value(12345.678, precision=0) == "12,346"

    def test_precision_four(self):
        """When the catalog specifies ``precision=4``, the value should
        show exactly 4 decimal places."""
        assert _format_signal_value(1.23456, precision=4) == "1.2346"

    def test_integer_no_decimals(self):
        """Integer values should be formatted with thousands separators
        but no decimal point."""
        assert _format_signal_value(12345) == "12,345"

    def test_string_passthrough(self):
        """String values pass through unchanged — no numeric formatting
        is applied."""
        assert _format_signal_value("Autosize") == "Autosize"

    def test_bool_as_string(self):
        """Boolean values (which are an ``int`` subclass in Python)
        should be converted to their string representation, not
        formatted as numbers."""
        assert _format_signal_value(value=True) == "True"
        assert _format_signal_value(value=False) == "False"
