"""
Tests for the sync_validators management command.

This command syncs system validators (EnergyPlus, FMU, THERM) and their catalog
entries from config declarations in each validator package to the database.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
from validibot.validations.validators.base.config import discover_configs


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

        self.assertEqual(validator.name, "EnergyPlus Validator")
        self.assertEqual(validator.validation_type, ValidationType.ENERGYPLUS)
        self.assertTrue(validator.is_system)
        self.assertTrue(validator.has_processor)
        self.assertEqual(validator.processor_name, "EnergyPlus Simulation")

    def test_command_creates_catalog_entries(self):
        """Test that catalog entries are created for validators."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        # Should have both input and output catalog entries
        input_entries = ValidatorCatalogEntry.objects.filter(
            validator=validator,
            entry_type=CatalogEntryType.SIGNAL,
            run_stage="input",
        )
        output_entries = ValidatorCatalogEntry.objects.filter(
            validator=validator,
            entry_type=CatalogEntryType.SIGNAL,
            run_stage="output",
        )

        self.assertTrue(input_entries.exists(), "Should have input signal entries")
        self.assertTrue(output_entries.exists(), "Should have output signal entries")

    def test_command_creates_specific_output_signals(self):
        """Test that specific output signals are created."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        # Check for specific output signals
        expected_signals = [
            "site_electricity_kwh",
            "site_eui_kwh_m2",
            "unmet_heating_hours",
            "floor_area_m2",
        ]

        for slug in expected_signals:
            self.assertTrue(
                ValidatorCatalogEntry.objects.filter(
                    validator=validator,
                    slug=slug,
                ).exists(),
                f"Signal {slug} should exist",
            )

    def test_command_creates_derivation_entries(self):
        """Test that derivation entries are created."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        derivations = ValidatorCatalogEntry.objects.filter(
            validator=validator,
            entry_type=CatalogEntryType.DERIVATION,
        )

        self.assertTrue(derivations.exists(), "Should have derivation entries")

        # Check for specific derivation
        total_unmet = ValidatorCatalogEntry.objects.filter(
            validator=validator,
            slug="total_unmet_hours",
            entry_type=CatalogEntryType.DERIVATION,
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
        self.assertEqual(validator.name, "EnergyPlus Validator")

    def test_command_reports_creation_counts(self):
        """Test that command reports how many validators/entries were created."""
        out, _ = self.call_command()

        # Should report validators created
        self.assertIn("validators created", out.lower())

        # Should report catalog entries created
        self.assertIn("catalog entries created", out.lower())


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
