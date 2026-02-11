"""
Tests for the sync_advanced_validators management command.

This command syncs advanced validators (EnergyPlus, FMI) and their catalog
entries from seed data in validibot.validations.seeds to the database.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
from validibot.validations.seeds import SYSTEM_VALIDATOR_SEEDS


class SyncAdvancedValidatorsCommandTests(TestCase):
    """Tests for the sync_advanced_validators management command."""

    def call_command(self, *args, **kwargs):
        """Helper to call the command and capture output."""
        out = StringIO()
        err = StringIO()
        call_command(
            "sync_advanced_validators",
            *args,
            stdout=out,
            stderr=err,
            **kwargs,
        )
        return out.getvalue(), err.getvalue()

    def test_command_creates_validators_from_seeds(self):
        """Test that command creates validators from seed data."""
        out, _ = self.call_command()

        # Verify output mentions sync complete
        self.assertIn("Sync complete", out)

        # Verify validators were created
        for seed in SYSTEM_VALIDATOR_SEEDS:
            slug = seed["validator"]["slug"]
            self.assertTrue(
                Validator.objects.filter(slug=slug).exists(),
                f"Validator {slug} should exist",
            )

    def test_command_creates_energyplus_validator(self):
        """Test that EnergyPlus validator is created with correct attributes."""
        self.call_command()

        validator = Validator.objects.get(slug="energyplus-idf-validator")

        self.assertEqual(validator.name, "EnergyPlus Validation")
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
        for seed in SYSTEM_VALIDATOR_SEEDS:
            slug = seed["validator"]["slug"]
            count = Validator.objects.filter(slug=slug).count()
            self.assertEqual(
                count,
                1,
                f"Should have exactly 1 validator with slug {slug}, found {count}",
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
        self.assertEqual(validator.name, "EnergyPlus Validation")

    def test_command_reports_creation_counts(self):
        """Test that command reports how many validators/entries were created."""
        out, _ = self.call_command()

        # Should report validators created
        self.assertIn("validators created", out.lower())

        # Should report catalog entries created
        self.assertIn("catalog entries created", out.lower())
