"""
Tests for the sync_validators management command.

This command syncs system validators (EnergyPlus, FMU, THERM) and their catalog
entries from config declarations in each validator package to the database.
"""

from io import StringIO

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase
from django.test import override_settings

from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.models import Derivation
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator
from validibot.validations.validators.base.config import discover_configs
from validibot.validations.validators.base.config import get_all_configs
from validibot.validations.validators.base.config import get_config


class SyncValidatorsCommandTests(TestCase):
    """Tests for the sync_validators management command."""

    def call_command(self, *args, **kwargs):
        """Helper to call the command and capture output."""
        out = StringIO()
        err = StringIO()
        call_command(
            "sync_validators",
            *args,
            stdout=out,
            stderr=err,
            **kwargs,
        )
        return out.getvalue(), err.getvalue()

    def test_command_creates_validators_from_configs(self):
        """Test that command creates validators from discovered configs."""
        out, _ = self.call_command()

        # Verify output mentions sync complete
        self.assertIn("Sync complete", out)

        # Verify validators were created for each discovered config
        for cfg in discover_configs():
            self.assertTrue(
                Validator.objects.filter(slug=cfg.slug).exists(),
                f"Validator {cfg.slug} should exist",
            )

    def test_command_creates_energyplus_validator(self):
        """Test that EnergyPlus validator is created with correct attributes."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        self.assertEqual(validator.name, "EnergyPlus™ Validator")
        self.assertEqual(validator.validation_type, ValidationType.ENERGYPLUS)
        self.assertTrue(validator.is_system)
        self.assertTrue(validator.has_processor)
        self.assertTrue(validator.supports_assertions)
        self.assertEqual(validator.processor_name, "EnergyPlus™ Simulation")

    def test_command_creates_signal_definitions(self):
        """Test that signal definitions are created for validators."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        # Should have both input and output signal definitions
        input_sigs = SignalDefinition.objects.filter(
            validator=validator,
            direction="input",
        )
        output_sigs = SignalDefinition.objects.filter(
            validator=validator,
            direction="output",
        )

        self.assertTrue(input_sigs.exists(), "Should have input signal definitions")
        self.assertTrue(output_sigs.exists(), "Should have output signal definitions")

    def test_command_creates_specific_output_signals(self):
        """Test that specific output signal definitions are created."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        # Check for specific output signals
        expected_signals = [
            "site_electricity_kwh",
            "site_eui_kwh_m2",
            "unmet_heating_hours",
            "floor_area_m2",
        ]

        for key in expected_signals:
            self.assertTrue(
                SignalDefinition.objects.filter(
                    validator=validator,
                    contract_key=key,
                ).exists(),
                f"Signal {key} should exist",
            )

    def test_command_normalizes_energyplus_input_provider_binding(self):
        """EnergyPlus input signals should not persist submission-source
        selectors in SignalDefinition.provider_binding.

        The unified signal model stores submission sourcing on
        StepSignalBinding, so sync_validators must strip legacy
        ``source``/``path`` keys from provider_binding.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")
        signal = SignalDefinition.objects.get(
            validator=validator,
            contract_key="expected_floor_area_m2",
            direction="input",
        )

        self.assertEqual(signal.provider_binding, {})

    def test_command_normalizes_energyplus_output_provider_binding(self):
        """EnergyPlus output provider binding should store the canonical
        ``metric_key`` field used by the unified signal model.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")
        signal = SignalDefinition.objects.get(
            validator=validator,
            contract_key="site_eui_kwh_m2",
            direction="output",
        )

        self.assertEqual(
            signal.provider_binding,
            {"metric_key": "site_eui_kwh_m2"},
        )

    def test_command_creates_derivations(self):
        """Test that derivation entries are created."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        derivations = Derivation.objects.filter(
            validator=validator,
        )

        self.assertTrue(derivations.exists(), "Should have derivation entries")

        # Check for specific derivation
        total_unmet = Derivation.objects.filter(
            validator=validator,
            contract_key="total_unmet_hours",
        )
        self.assertTrue(
            total_unmet.exists(),
            "total_unmet_hours derivation should exist",
        )

    def test_command_is_idempotent(self):
        """Test that running command multiple times doesn't create duplicates."""
        # Run twice
        self.call_command()
        self.call_command()

        # Should only have one of each validator
        for cfg in discover_configs():
            count = Validator.objects.filter(slug=cfg.slug).count()
            self.assertEqual(
                count,
                1,
                f"Should have exactly 1 validator with slug {cfg.slug}, found {count}",
            )

    def test_command_updates_existing_validator(self):
        """Test that command updates existing validator fields."""
        # Create a validator with different name
        Validator.objects.create(
            slug="energyplus-idf-validator",
            name="Old Name",
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )

        out, _ = self.call_command()

        # Verify output mentions update
        self.assertIn("Updated", out)

        # Verify name was updated
        validator = Validator.objects.get(slug="energyplus-idf-validator")
        self.assertEqual(validator.name, "EnergyPlus™ Validator")

    def test_command_syncs_supports_assertions(self):
        """Test that supports_assertions is synced from config to DB."""
        self.call_command()

        # Validators that support assertions
        for slug in (
            "basic-validator",
            "energyplus-idf-validator",
            "fmu-validator",
            "ai-assisted-validator",
            "therm-validator",
        ):
            validator = Validator.objects.get(slug=slug)
            self.assertTrue(
                validator.supports_assertions,
                f"{slug} should support assertions",
            )

        # Validators that do NOT support assertions (schema-only)
        for slug in ("json-schema-validator", "xml-validator"):
            validator = Validator.objects.get(slug=slug)
            self.assertFalse(
                validator.supports_assertions,
                f"{slug} should not support assertions",
            )

    def test_command_reports_creation_counts(self):
        """Test that command reports how many validators/entries were created."""
        out, _ = self.call_command()

        # Should report validators created
        self.assertIn("validators created", out.lower())

        # Should report signals synced
        self.assertIn("signals synced", out.lower())

    # ── source_kind and is_path_editable ────────────────────────────
    # These tests verify that the sync command correctly persists the
    # new source metadata fields from CatalogEntrySpec to SignalDefinition.

    def test_energyplus_input_signals_are_internal_and_not_editable(self):
        """EnergyPlus input signals should be INTERNAL + non-editable.

        The validator controls where its inputs come from (fixed submission
        metadata paths). Authors should not be able to change these paths.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")
        input_sigs = SignalDefinition.objects.filter(
            validator=validator,
            direction="input",
        )

        self.assertTrue(input_sigs.exists())
        for sig in input_sigs:
            self.assertEqual(
                sig.source_kind,
                SignalSourceKind.INTERNAL,
                f"EnergyPlus input signal {sig.contract_key} should be INTERNAL",
            )
            self.assertFalse(
                sig.is_path_editable,
                f"EnergyPlus input signal {sig.contract_key}"
                " should not be path-editable",
            )

    def test_energyplus_output_signals_are_internal_and_not_editable(self):
        """EnergyPlus output signals should be INTERNAL + non-editable.

        Output values come from simulation metrics extracted internally
        by the validator — the author has no control over the extraction.
        """
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")
        output_sigs = SignalDefinition.objects.filter(
            validator=validator,
            direction="output",
        )

        self.assertTrue(output_sigs.exists())
        for sig in output_sigs:
            self.assertEqual(
                sig.source_kind,
                SignalSourceKind.INTERNAL,
                f"EnergyPlus output signal {sig.contract_key} should be INTERNAL",
            )
            self.assertFalse(
                sig.is_path_editable,
                f"EnergyPlus output signal {sig.contract_key}"
                " should not be path-editable",
            )

    def test_therm_output_signals_are_internal_and_not_editable(self):
        """THERM output signals should be INTERNAL + non-editable.

        THERM extracts values directly from the THMX/THMZ XML — the
        author has no control over the extraction paths.
        """
        self.call_command()

        validator = Validator.objects.get(slug="therm-validator")
        output_sigs = SignalDefinition.objects.filter(
            validator=validator,
            direction="output",
        )

        self.assertTrue(output_sigs.exists())
        for sig in output_sigs:
            self.assertEqual(
                sig.source_kind,
                SignalSourceKind.INTERNAL,
                f"THERM output signal {sig.contract_key} should be INTERNAL",
            )
            self.assertFalse(
                sig.is_path_editable,
                f"THERM output signal {sig.contract_key} should not be path-editable",
            )


class CreateDefaultValidatorsTests(TestCase):
    """Tests for create_default_validators() in utils.py."""

    def test_sets_supports_assertions(self):
        """create_default_validators sets supports_assertions from config."""
        from validibot.validations.utils import create_default_validators

        create_default_validators()

        # Validators that support assertions
        for slug in (
            "basic-validator",
            "energyplus-idf-validator",
            "fmu-validator",
            "ai-assisted-validator",
            "therm-validator",
        ):
            validator = Validator.objects.get(slug=slug)
            self.assertTrue(
                validator.supports_assertions,
                f"{slug} should support assertions",
            )

        # Validators that do NOT support assertions
        for slug in ("json-schema-validator", "xml-validator"):
            validator = Validator.objects.get(slug=slug)
            self.assertFalse(
                validator.supports_assertions,
                f"{slug} should not support assertions",
            )


class DiscoverConfigsTests(TestCase):
    """Tests for the discover_configs() function."""

    def test_discovers_system_validators(self):
        """discover_configs() finds configs for all system validators."""
        configs = discover_configs()
        slugs = {c.slug for c in configs}

        self.assertIn("energyplus-idf-validator", slugs)
        self.assertIn("therm-validator", slugs)
        self.assertIn("fmu-validator", slugs)

    def test_configs_are_sorted(self):
        """Configs are returned sorted by (order, name)."""
        configs = discover_configs()
        orders = [(c.order, c.name) for c in configs]

        self.assertEqual(orders, sorted(orders))

    def test_configs_have_required_fields(self):
        """Every config has slug, name, and validation_type."""
        for cfg in discover_configs():
            self.assertTrue(cfg.slug, f"Config {cfg} should have a slug")
            self.assertTrue(cfg.name, f"Config {cfg} should have a name")
            self.assertTrue(
                cfg.validation_type,
                f"Config {cfg} should have a validation_type",
            )

    def test_energyplus_has_catalog_entries(self):
        """EnergyPlus config has input/output signals and derivations."""
        configs = discover_configs()
        ep_config = next(c for c in configs if c.slug == "energyplus-idf-validator")

        self.assertGreater(len(ep_config.catalog_entries), 0)

        entry_types = {e.entry_type for e in ep_config.catalog_entries}
        self.assertIn("signal", entry_types)
        self.assertIn("derivation", entry_types)

    def test_fmu_has_no_catalog_entries(self):
        """FMU config has no static catalog entries (entries are dynamic)."""
        configs = discover_configs()
        fmu_config = next(c for c in configs if c.slug == "fmu-validator")

        self.assertEqual(len(fmu_config.catalog_entries), 0)

    def test_energyplus_has_file_handling_fields(self):
        """EnergyPlus config has file type and extension fields populated."""
        configs = discover_configs()
        ep_config = next(c for c in configs if c.slug == "energyplus-idf-validator")

        self.assertIn("text", ep_config.supported_file_types)
        self.assertIn("energyplus_idf", ep_config.supported_data_formats)
        self.assertIn("idf", ep_config.allowed_extensions)
        self.assertIn("energyplus_weather", ep_config.resource_types)

    def test_configs_have_display_metadata(self):
        """All discovered configs have icon and card_image set."""
        for cfg in discover_configs():
            self.assertTrue(cfg.icon, f"{cfg.slug} missing icon")
            self.assertTrue(cfg.card_image, f"{cfg.slug} missing card_image")

    def test_discovered_configs_include_provider_metadata(self):
        """Discovered validator configs should record the provider module."""

        for cfg in discover_configs():
            self.assertTrue(
                cfg.provider,
                f"{cfg.slug} should include a provider module path",
            )

    @override_settings(
        VALIDIBOT_ALLOWED_VALIDATOR_PLUGIN_PREFIXES=(
            "validibot.validations.validators.base",
        ),
    )
    def test_discover_configs_rejects_disallowed_provider_prefixes(self):
        """Discovery should fail when a validator provider is not allowlisted."""

        with pytest.raises(ImproperlyConfigured):
            discover_configs()


class ConfigRegistryTests(TestCase):
    """Tests for the config registry (populated at Django startup)."""

    def test_all_validation_types_registered(self):
        """Every ValidationType has a config in the registry."""
        for vtype in ValidationType:
            cfg = get_config(vtype.value)
            self.assertIsNotNone(cfg, f"No config registered for {vtype}")

    def test_get_config_returns_correct_type(self):
        """get_config() returns the matching config for a known type."""
        cfg = get_config(ValidationType.ENERGYPLUS)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.slug, "energyplus-idf-validator")

    def test_get_config_nonexistent_returns_none(self):
        """get_config() returns None for an unregistered type."""
        self.assertIsNone(get_config("NONEXISTENT"))

    def test_get_all_configs_sorted(self):
        """get_all_configs() returns configs sorted by (order, name)."""
        configs = get_all_configs()
        orders = [(c.order, c.name) for c in configs]
        self.assertEqual(orders, sorted(orders))

    def test_get_all_configs_includes_builtins_and_discovered(self):
        """Registry includes both built-in and discovered configs."""
        configs = get_all_configs()
        slugs = {c.slug for c in configs}

        # Built-in validators
        self.assertIn("basic-validator", slugs)
        self.assertIn("json-schema-validator", slugs)

        # Discovered validators
        self.assertIn("energyplus-idf-validator", slugs)
        self.assertIn("therm-validator", slugs)

    def test_energyplus_has_resource_types(self):
        """EnergyPlus config declares weather resource type."""
        cfg = get_config(ValidationType.ENERGYPLUS)
        self.assertIn("energyplus_weather", cfg.resource_types)

    def test_basic_has_json_file_type(self):
        """Basic config declares JSON as supported file type."""
        cfg = get_config(ValidationType.BASIC)
        self.assertIn("json", cfg.supported_file_types)

    def test_configs_declare_supports_assertions(self):
        """Configs correctly declare assertion support."""
        assertion_slugs = {
            "basic-validator",
            "energyplus-idf-validator",
            "fmu-validator",
            "ai-assisted-validator",
            "therm-validator",
            "custom-validator",
        }
        no_assertion_slugs = {"json-schema-validator", "xml-validator"}

        for cfg in get_all_configs():
            if cfg.slug in assertion_slugs:
                self.assertTrue(
                    cfg.supports_assertions,
                    f"Config {cfg.slug} should declare supports_assertions=True",
                )
            elif cfg.slug in no_assertion_slugs:
                self.assertFalse(
                    cfg.supports_assertions,
                    f"Config {cfg.slug} should declare supports_assertions=False",
                )

    def test_registry_count(self):
        """Registry has exactly 8 configs (one per ValidationType)."""
        configs = get_all_configs()
        self.assertEqual(len(configs), len(ValidationType))

    def test_registry_configs_include_provider_metadata(self):
        """Registry entries should keep the resolved provider module."""

        for cfg in get_all_configs():
            self.assertTrue(
                cfg.provider,
                f"{cfg.slug} should keep provider metadata in the registry",
            )


class CreateCustomValidatorTests(TestCase):
    """Tests that create_custom_validator() sets assertion support correctly."""

    def test_custom_validator_supports_assertions(self):
        """create_custom_validator() sets supports_assertions=True."""
        from validibot.users.tests.factories import OrganizationFactory
        from validibot.users.tests.factories import UserFactory
        from validibot.validations.constants import CustomValidatorType
        from validibot.validations.utils import create_custom_validator

        org = OrganizationFactory()
        user = UserFactory()

        custom = create_custom_validator(
            org=org,
            user=user,
            name="Test Custom Validator",
            short_description="A test custom validator",
            description="Test description",
            custom_type=CustomValidatorType.SIMPLE,
        )
        self.assertTrue(
            custom.validator.supports_assertions,
            "Custom validators should have supports_assertions=True",
        )


class BasicValidatorConfigRuntimeAlignmentTests(TestCase):
    """Verify that Basic validator config file types match runtime."""

    def test_config_file_types_match_runtime(self):
        """Basic validator config should only advertise file types the runtime supports.

        The runtime (BasicValidator._SUPPORTED_FILE_TYPES) is the source
        of truth. The config should not advertise broader support, which
        would let workflows accept submissions that later fail at runtime.
        """
        from validibot.validations.validators.basic import BasicValidator

        cfg = get_config(ValidationType.BASIC)
        config_types = set(cfg.supported_file_types)
        runtime_types = BasicValidator._SUPPORTED_FILE_TYPES

        self.assertEqual(
            config_types,
            runtime_types,
            "Config file types should match runtime _SUPPORTED_FILE_TYPES",
        )
