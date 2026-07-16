"""
Tests for the unified validator registry.

``ValidatorConfig`` is the single source of truth for each validator,
carrying both metadata and resolved class references. At startup,
``register_validators()`` discovers all community validators and
registers each via ``register_validator_config()``.

These tests verify:

1. Every ``ValidationType`` has a registered config with a resolved class.
2. ``get_validator_class()`` returns the correct class for each type.
3. ``get_config()`` returns the correct metadata for each type.
4. ``discover_configs()`` finds all validator sub-packages.
5. ``register_validator_config()`` correctly registers individual configs
   and enforces provider allowlisting, duplicate detection, and import
   resolution.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import _CONFIG_REGISTRY
from validibot.validations.validators.base.config import ValidatorConfig
from validibot.validations.validators.base.config import discover_configs
from validibot.validations.validators.base.config import get_all_configs
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.base.config import get_output_envelope_class
from validibot.validations.validators.base.config import get_validator_class
from validibot.validations.validators.base.config import register_validator_config

# ══════════════════════════════════════════════════════════════════════════════
# Registry population — every ValidationType has config and resolved class
# ══════════════════════════════════════════════════════════════════════════════


class TestRegistryPopulation:
    """Verify that startup populates the registry for all system validators.

    The ``register_validators()`` call runs in ``ValidationsConfig.ready()``,
    so by the time tests execute, the registry should already be populated.
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
        a corresponding ``ValidatorConfig`` — fix by adding a ``config.py``
        in the validator's sub-package.
        """
        cfg = get_config(vtype.value)
        assert cfg is not None, f"No ValidatorConfig registered for {vtype.value}"
        assert cfg.validation_type == vtype.value

    @pytest.mark.parametrize(
        "vtype",
        list(ValidationType),
        ids=[v.value for v in ValidationType],
    )
    def test_every_validation_type_has_resolved_class(self, vtype):
        """Each ValidationType has a resolved validator class on its config.

        The ``resolved_class`` field is set at registration time by
        ``register_validator_config()``. If this fails, the
        ``validator_class`` dotted path is missing or unresolvable.
        """
        cfg = get_config(vtype.value)
        assert cfg is not None
        assert cfg.resolved_class is not None, (
            f"No resolved_class on config for {vtype.value}"
        )

    @pytest.mark.parametrize(
        "vtype",
        list(ValidationType),
        ids=[v.value for v in ValidationType],
    )
    def test_get_validator_class_matches_resolved_class(self, vtype):
        """get_validator_class() returns the same object as config.resolved_class.

        Both paths should yield the identical class reference — they read
        from the same registry entry.
        """
        cfg = get_config(vtype.value)
        cls = get_validator_class(vtype)
        assert cls is cfg.resolved_class

    @pytest.mark.parametrize(
        "vtype",
        list(ValidationType),
        ids=[v.value for v in ValidationType],
    )
    def test_class_has_validation_type_attribute(self, vtype):
        """Each resolved class has ``validation_type`` set to match its config.

        ``register_validator_config()`` sets ``cls.validation_type`` after
        resolving the dotted path. This attribute is used at runtime to
        identify which type a validator instance belongs to.
        """
        cls = get_validator_class(vtype)
        assert cls.validation_type == vtype.value

    @pytest.mark.parametrize(
        "vtype",
        list(ValidationType),
        ids=[v.value for v in ValidationType],
    )
    def test_resolved_class_matches_dotted_path(self, vtype):
        """The resolved class matches the validator_class dotted path.

        This ensures ``register_validator_config()`` correctly resolved
        the dotted path and stored the right class object.
        """
        cfg = get_config(vtype.value)
        if cfg and cfg.validator_class and cfg.resolved_class:
            cls = cfg.resolved_class
            assert f"{cls.__module__}.{cls.__qualname__}" == cfg.validator_class


# ══════════════════════════════════════════════════════════════════════════════
# Discovery — discover_configs() finds all validator packages
# ══════════════════════════════════════════════════════════════════════════════


class TestDiscoverConfigs:
    """Verify that discover_configs() finds all validator packages.

    Every validator lives in its own sub-package with a ``config.py`` module.
    ``discover_configs()`` should find all of them via directory walking.
    """

    def test_discovers_all_builtin_validator_types(self):
        """discover_configs() returns a config for every built-in ValidationType.

        ``ValidationType`` remains a convenience enum for validators that ship
        in the community repo. It is no longer the closed universe of all
        validator types, so plugin validators may register additional strings.
        """
        configs = discover_configs()
        discovered_types = {c.validation_type for c in configs}
        expected_types = {v.value for v in ValidationType}
        assert expected_types.issubset(discovered_types)

    def test_each_config_has_valid_validator_class(self):
        """Every discovered config has a non-empty validator_class path.

        The validator_class dotted path is resolved at registration time
        via import_string(). A missing or empty path would silently skip
        class registration.
        """
        for cfg in discover_configs():
            assert cfg.validator_class, f"Config '{cfg.slug}' has no validator_class"

    def test_configs_sorted_by_order_then_name(self):
        """discover_configs() returns configs sorted by (order, name).

        This ordering is used in the UI when listing available validators.
        """
        configs = discover_configs()
        expected = sorted(configs, key=lambda c: (c.order, c.name))
        assert [c.slug for c in configs] == [c.slug for c in expected]


# ══════════════════════════════════════════════════════════════════════════════
# Config registry — metadata lookups
# ══════════════════════════════════════════════════════════════════════════════


class TestConfigRegistry:
    """Verify config registry metadata lookups."""

    def test_get_all_configs_returns_all_builtin_types(self):
        """``get_all_configs()`` returns a config for every built-in type.

        Built-in constants remain useful for application code, but external
        packages can register additional validation_type strings at runtime.
        """
        all_configs = get_all_configs()
        registered_types = {c.validation_type for c in all_configs}
        expected_types = {v.value for v in ValidationType}
        assert expected_types.issubset(registered_types)

    def test_get_config_returns_none_for_unknown(self):
        """``get_config()`` returns None for an unregistered type.

        Callers should handle None gracefully — e.g. dynamically created
        custom validators may not have a config.
        """
        assert get_config("NONEXISTENT_TYPE") is None

    def test_get_validator_class_raises_for_unknown(self):
        """``get_validator_class()`` raises KeyError for an unregistered type.

        Runtime callers should catch this or ensure the type is valid
        before calling.
        """
        with pytest.raises(KeyError):
            get_validator_class("NONEXISTENT_TYPE")

    def test_get_output_envelope_class_returns_none_for_unknown(self):
        """``get_output_envelope_class()`` returns None for an unregistered type.

        Callers should handle None gracefully — not all validators use
        container-based envelopes.
        """
        assert get_output_envelope_class("NONEXISTENT_TYPE") is None


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

        cls = get_validator_class(ValidationType.BASIC)
        assert cls is BasicValidator

    def test_energyplus_validator_class(self):
        """EnergyPlusValidator is resolved for ENERGYPLUS type."""
        from validibot.validations.validators.energyplus.validator import (
            EnergyPlusValidator,
        )

        cls = get_validator_class(ValidationType.ENERGYPLUS)
        assert cls is EnergyPlusValidator

    def test_custom_validator_class(self):
        """CustomValidator is resolved for CUSTOM_VALIDATOR type."""
        from validibot.validations.validators.custom.validator import CustomValidator

        cls = get_validator_class(ValidationType.CUSTOM_VALIDATOR)
        assert cls is CustomValidator


# ══════════════════════════════════════════════════════════════════════════════
# StepEditorCardSpec — UI extension declarations
# ══════════════════════════════════════════════════════════════════════════════


class TestStepEditorCards:
    """Verify step editor card specs on validator configs.

    Template variable editing is handled by the unified Inputs/Outputs card
    rather than custom step_editor_cards. No validators currently
    declare custom cards, but the extension point remains available
    for future use.
    """

    def test_energyplus_has_no_step_editor_cards(self):
        """EnergyPlus no longer declares custom step editor cards.

        Template variable editing moved to the unified Inputs/Outputs card's
        per-variable edit modal.
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


# ══════════════════════════════════════════════════════════════════════════════
# Envelope class resolution
# ══════════════════════════════════════════════════════════════════════════════


class TestEnvelopeClassResolution:
    """Verify output envelope class resolution for container-based validators.

    Only advanced validators that run in containers (EnergyPlus, FMU, Custom)
    declare an ``output_envelope_class``. The resolved class is stored on the
    config at registration time and used to deserialize container output.
    """

    def test_custom_validator_has_envelope_class(self):
        """Custom validator declares an output envelope class.

        The CustomValidator uses container-based execution and needs an
        envelope class to deserialize its output.json.
        """
        cfg = get_config(ValidationType.CUSTOM_VALIDATOR)
        assert cfg is not None
        assert cfg.resolved_envelope_class is not None

    def test_basic_validator_has_no_envelope_class(self):
        """Basic validator has no envelope class.

        Built-in validators that run in-process don't use container
        envelopes.
        """
        assert get_output_envelope_class(ValidationType.BASIC) is None

    def test_get_output_envelope_class_matches_config(self):
        """get_output_envelope_class() returns the same object as
        config.resolved_envelope_class.
        """
        cfg = get_config(ValidationType.CUSTOM_VALIDATOR)
        envelope_cls = get_output_envelope_class(ValidationType.CUSTOM_VALIDATOR)
        assert envelope_cls is cfg.resolved_envelope_class


# ══════════════════════════════════════════════════════════════════════════════
# register_validator_config() — individual config registration
#
# These tests verify the public API that external packages (validibot-pro,
# validibot-enterprise) use to register their own validators from their
# AppConfig.ready() methods.
# ══════════════════════════════════════════════════════════════════════════════

TEST_VALIDATION_TYPE = "TEST_REGISTER_SINGLE"


@pytest.fixture
def _cleanup_test_registry():
    """Remove test entries from the registry after each test.

    The registry is a module-level dict that persists across tests, so
    any entries added during a test must be cleaned up to avoid leaking
    state into subsequent tests.
    """
    yield
    _CONFIG_REGISTRY.pop(TEST_VALIDATION_TYPE, None)


@pytest.mark.usefixtures("_cleanup_test_registry")
class TestRegisterValidatorConfig:
    """Verify register_validator_config() registers individual configs.

    This is the public API for external packages to register validators,
    mirroring how register_action_descriptor() works for actions.
    """

    def _make_test_config(self, **overrides) -> ValidatorConfig:
        """Create a minimal ValidatorConfig for testing."""
        defaults = {
            "slug": "test-validator",
            "name": "Test Validator",
            "validation_type": TEST_VALIDATION_TYPE,
            "validator_class": (
                "validibot.validations.validators.basic.BasicValidator"
            ),
            "provider": "validibot.validations.tests",
        }
        defaults.update(overrides)
        return ValidatorConfig(**defaults)

    def test_registers_config_with_resolved_class(self):
        """A registered config has its resolved_class populated.

        This is the core contract: calling register_validator_config()
        resolves the validator_class dotted path and stores both the
        metadata and the resolved class on a single config object.
        """
        config = self._make_test_config()
        register_validator_config(config)

        stored = _CONFIG_REGISTRY[TEST_VALIDATION_TYPE]
        assert stored.slug == "test-validator"
        assert stored.resolved_class is not None

    def test_get_validator_class_works_after_registration(self):
        """get_validator_class() returns the resolved class after registration.

        This verifies the end-to-end path: register a config, then look
        it up by validation_type.
        """
        config = self._make_test_config()
        register_validator_config(config)

        cls = get_validator_class(TEST_VALIDATION_TYPE)
        from validibot.validations.validators.basic import BasicValidator

        assert cls is BasicValidator

    def test_sets_validation_type_on_class(self):
        """The resolved class gets its validation_type attribute set.

        This attribute is used at runtime to identify which type a
        validator instance belongs to.
        """
        config = self._make_test_config()
        register_validator_config(config)

        cls = get_validator_class(TEST_VALIDATION_TYPE)
        assert cls.validation_type == TEST_VALIDATION_TYPE

    def test_duplicate_raises_value_error(self):
        """Registering the same validation_type twice raises ValueError.

        This prevents silent overwrites when two packages accidentally
        claim the same type string.
        """
        config = self._make_test_config()
        register_validator_config(config)

        duplicate = self._make_test_config(slug="duplicate-validator")
        with pytest.raises(ValueError, match="Duplicate config registration"):
            register_validator_config(duplicate)

    def test_disallowed_provider_raises(self):
        """A provider outside the allowlist is rejected.

        Only packages from official namespaces (validibot, validibot_pro,
        validibot_enterprise) can register validators by default.
        """
        config = self._make_test_config(provider="evil_package.validators")
        with pytest.raises(ImproperlyConfigured, match="not allowed"):
            register_validator_config(config)

    def test_bad_import_path_raises(self):
        """An unresolvable validator_class path raises ImportError.

        The error message includes the config slug and validation_type
        so operators can quickly identify the misconfigured package.
        """
        config = self._make_test_config(
            validator_class="nonexistent.module.FakeValidator",
        )
        with pytest.raises(ImportError, match="Cannot import validator class"):
            register_validator_config(config)

    def test_infers_provider_from_validator_class(self):
        """When provider is empty, it is inferred from validator_class.

        This matches how discover_configs() infers providers for
        package-based validators that don't set provider explicitly.
        """
        config = self._make_test_config(provider="")
        register_validator_config(config)

        stored = _CONFIG_REGISTRY[TEST_VALIDATION_TYPE]
        assert stored.provider == "validibot.validations.validators.basic"

    def test_registers_envelope_class_on_config(self):
        """A config with output_envelope_class has resolved_envelope_class set.

        Only advanced (container) validators use envelope classes, but
        the registration path must resolve and store them correctly.
        """
        config = self._make_test_config(
            output_envelope_class=(
                "validibot_shared.validations.envelopes.ValidationOutputEnvelope"
            ),
        )
        register_validator_config(config)

        stored = _CONFIG_REGISTRY[TEST_VALIDATION_TYPE]
        assert stored.resolved_envelope_class is not None

    def test_bad_envelope_import_path_raises(self):
        """An unresolvable output_envelope_class path raises ImportError.

        The error message includes the config slug so operators can
        quickly identify the misconfigured package.
        """
        config = self._make_test_config(
            output_envelope_class="nonexistent.module.FakeEnvelope",
        )
        with pytest.raises(ImportError, match="Cannot import output envelope class"):
            register_validator_config(config)


# ══════════════════════════════════════════════════════════════════════════════
# Cloud Run Job name resolution (May 2026 standardisation)
# ══════════════════════════════════════════════════════════════════════════════
#
# ``ValidatorConfig.cloud_run_job_name`` is the canonical source for the
# GCP Cloud Run Job resource name used by ``cloud_run/launcher`` at
# runtime. It replaces the legacy ``settings.GCS_ENERGYPLUS_JOB_NAME`` /
# ``GCS_FMU_JOB_NAME`` env vars (removed because they had been silently
# unset in prod Secret Manager for months, breaking validator dispatch).
#
# These tests pin the contract so a regression that drops the property,
# changes the fallback convention, or splits image_name vs job_name
# without updating both deploy recipes surfaces immediately.


class TestCloudRunJobName:
    """``ValidatorConfig.cloud_run_job_name`` derivation rules.

    By project convention, the Cloud Run Job name equals the Docker
    image name (see ``ValidatorConfig.image_name`` docstring). The
    property exposes that with an intent-revealing name so callers
    read ``config.cloud_run_job_name`` rather than ``config.image_name``
    even though they're the same string today — keeps room to split
    them later without touching every caller.
    """

    def test_returns_image_name_when_set(self):
        """Explicit ``image_name`` wins over the convention fallback.

        Every system advanced validator (EnergyPlus, FMU) sets
        ``image_name`` in its config.py, so this is the normal path.
        """
        config = ValidatorConfig(
            slug="example",
            name="Example",
            validation_type="EXAMPLE",
            validator_class="validibot.validations.validators.basic.validator.BasicValidator",
            image_name="validibot-validator-backend-example",
        )
        assert config.cloud_run_job_name == "validibot-validator-backend-example"

    def test_falls_back_to_convention_when_image_name_empty(self):
        """No ``image_name`` → ``validibot-validator-backend-{slug}``.

        This matches the GCPExecutionBackend's image fallback so the
        runtime can resolve a name even for validators that didn't
        explicitly set image_name (e.g. user-created custom advanced
        validators). The slug here is ``validation_type.lower()`` so
        ``EXAMPLE`` → ``example``.
        """
        config = ValidatorConfig(
            slug="example",
            name="Example",
            validation_type="EXAMPLE",
            validator_class="validibot.validations.validators.basic.validator.BasicValidator",
            # image_name deliberately left as default empty string
        )
        assert config.cloud_run_job_name == "validibot-validator-backend-example"

    def test_system_validators_use_backend_prefix(self):
        """Every system advanced validator's job name uses the prefix.

        Pins the May 2026 naming standardisation: all
        ``validibot-validator-backend-*`` so the runtime, the catalog,
        and BOTH deploy paths (``validibot-validator-backends/justfile``
        and ``validibot/just/gcp/mod.just``) produce the same Cloud Run
        Job name. Drift here would re-introduce the bug where prod
        pointed at a job nobody deployed to.
        """
        for vtype in ("ENERGYPLUS", "FMU"):
            config = get_config(vtype)
            assert config is not None, f"{vtype} config not registered"
            assert config.cloud_run_job_name.startswith(
                "validibot-validator-backend-"
            ), (
                f"{vtype} cloud_run_job_name {config.cloud_run_job_name!r} "
                "does not follow the May 2026 standardised "
                "'validibot-validator-backend-{slug}' convention."
            )
