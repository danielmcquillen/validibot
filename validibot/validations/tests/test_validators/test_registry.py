"""
Tests for the unified validator registry.

The unified registry replaces the previous two-registry approach where
``ValidatorConfig`` metadata and ``@register_validator`` class bindings
were maintained separately.  Now ``ValidatorConfig`` is the single source
of truth: ``populate_registry()`` discovers all configs and populates
both the config registry (metadata lookups) and the validator class
registry (runtime instantiation) in a single pass.

These tests verify:

1. Both registries are populated from ``ValidatorConfig`` instances.
2. ``registry.get()`` resolves the correct validator class for each type.
3. ``get_config()`` returns the correct metadata for each type.
4. All system validators have both a config and a class registered.
"""

from __future__ import annotations

import pytest

from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import get_all_configs
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.base.registry import _VALIDATOR_REGISTRY
from validibot.validations.validators.base.registry import get

# ══════════════════════════════════════════════════════════════════════════════
# Registry population — every ValidationType has both config and class
# ══════════════════════════════════════════════════════════════════════════════


class TestRegistryPopulation:
    """Verify that startup populates both registries for all system validators.

    The ``populate_registry()`` call runs in ``ValidationsConfig.ready()``,
    so by the time tests execute, both registries should already be populated.
    These tests confirm that contract holds for every ``ValidationType``.
    """

    @pytest.mark.parametrize(
        "vtype",
        list(ValidationType),
        ids=[v.value for v in ValidationType],
    )
    def test_every_validation_type_has_config(self, vtype):
        """Each ValidationType has a matching ValidatorConfig in the registry.

        If this fails, a validator was added to ``ValidationType`` without
        a corresponding ``ValidatorConfig`` — fix by adding a config in
        the validator's ``config.py`` or in ``builtin_configs.py``.
        """
        cfg = get_config(vtype.value)
        assert cfg is not None, f"No ValidatorConfig registered for {vtype.value}"
        assert cfg.validation_type == vtype.value

    @pytest.mark.parametrize(
        "vtype",
        list(ValidationType),
        ids=[v.value for v in ValidationType],
    )
    def test_every_validation_type_has_class(self, vtype):
        """Each ValidationType has a validator class in the class registry.

        If this fails, the ``validator_class`` dotted path is missing from
        the ``ValidatorConfig`` for this type.
        """
        cls = get(vtype)
        assert cls is not None, f"No validator class registered for {vtype.value}"

    @pytest.mark.parametrize(
        "vtype",
        list(ValidationType),
        ids=[v.value for v in ValidationType],
    )
    def test_class_has_validation_type_attribute(self, vtype):
        """Each resolved class has ``validation_type`` set to match its config.

        ``populate_registry()`` sets ``cls.validation_type`` after resolving
        the dotted path.  This attribute is used at runtime to identify which
        type a validator instance belongs to.
        """
        cls = get(vtype)
        assert cls.validation_type == vtype.value


# ══════════════════════════════════════════════════════════════════════════════
# Config registry — metadata lookups
# ══════════════════════════════════════════════════════════════════════════════


class TestConfigRegistry:
    """Verify config registry metadata lookups."""

    def test_get_all_configs_returns_all_types(self):
        """``get_all_configs()`` returns a config for every ValidationType.

        This is a sanity check that nothing was lost during the registry
        unification — every type we define should have a config entry.
        """
        all_configs = get_all_configs()
        registered_types = {c.validation_type for c in all_configs}
        expected_types = {v.value for v in ValidationType}
        assert registered_types == expected_types

    def test_get_config_returns_none_for_unknown(self):
        """``get_config()`` returns None for an unregistered type.

        Callers should handle None gracefully — e.g. dynamically created
        custom validators may not have a config.
        """
        assert get_config("NONEXISTENT_TYPE") is None

    def test_class_registry_get_raises_for_unknown(self):
        """``registry.get()`` raises KeyError for an unregistered type.

        Runtime callers should catch this or ensure the type is valid
        before calling.
        """
        with pytest.raises(KeyError):
            get("NONEXISTENT_TYPE")


# ══════════════════════════════════════════════════════════════════════════════
# Specific validator class resolution
# ══════════════════════════════════════════════════════════════════════════════


class TestValidatorClassResolution:
    """Verify that specific validator classes are correctly resolved.

    Spot-checks a few validators to ensure the ``validator_class`` dotted
    path in their config resolves to the expected Python class.
    """

    def test_basic_validator_class(self):
        """BasicValidator is resolved for BASIC type."""
        from validibot.validations.validators.basic import BasicValidator

        cls = get(ValidationType.BASIC)
        assert cls is BasicValidator

    def test_energyplus_validator_class(self):
        """EnergyPlusValidator is resolved for ENERGYPLUS type."""
        from validibot.validations.validators.energyplus.validator import (
            EnergyPlusValidator,
        )

        cls = get(ValidationType.ENERGYPLUS)
        assert cls is EnergyPlusValidator

    def test_custom_validator_class(self):
        """CustomValidator is resolved for CUSTOM_VALIDATOR type."""
        from validibot.validations.validators.custom.validator import CustomValidator

        cls = get(ValidationType.CUSTOM_VALIDATOR)
        assert cls is CustomValidator


# ══════════════════════════════════════════════════════════════════════════════
# StepEditorCardSpec — UI extension declarations
# ══════════════════════════════════════════════════════════════════════════════


class TestStepEditorCards:
    """Verify step editor card specs on validator configs.

    Since ADR-2026-03-10 (Unified Input/Output Signals UI), template
    variable editing is handled by the unified signals card rather than
    custom step_editor_cards.  No validators currently declare custom
    cards, but the extension point remains available for future use.
    """

    def test_energyplus_has_no_step_editor_cards(self):
        """EnergyPlus no longer declares custom step editor cards.

        Template variable editing moved to the unified signals card's
        per-variable edit modal (ADR-2026-03-10).
        """
        cfg = get_config(ValidationType.ENERGYPLUS)
        assert cfg is not None
        assert cfg.step_editor_cards == []

    def test_basic_has_no_step_editor_cards(self):
        """Basic validator has no step editor cards.

        Most validators don't need custom UI on the step detail page.
        """
        cfg = get_config(ValidationType.BASIC)
        assert cfg is not None
        assert cfg.step_editor_cards == []

    def test_validator_class_registry_matches_config(self):
        """The class in _VALIDATOR_REGISTRY matches the config's dotted path.

        This ensures ``populate_registry()`` correctly resolved the dotted
        path and stored the same class object.
        """
        for vtype in ValidationType:
            cfg = get_config(vtype.value)
            if cfg and cfg.validator_class:
                cls = _VALIDATOR_REGISTRY.get(vtype.value)
                assert cls is not None
                assert f"{cls.__module__}.{cls.__qualname__}" == cfg.validator_class
