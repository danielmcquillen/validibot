"""
Tests for system validator seeds and sync functionality.
"""

import pytest
from django.core.management import call_command

from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
from validibot.validations.seeds import ENERGYPLUS_SEED


@pytest.mark.django_db
def test_sync_system_validators_creates_energyplus():
    """The sync command should create the EnergyPlus validator and catalog entries."""
    # Ensure we start clean
    Validator.objects.filter(slug="energyplus-idf-validator").delete()

    call_command("sync_system_validators")

    validator = Validator.objects.get(slug="energyplus-idf-validator")
    assert validator.validation_type == ValidationType.ENERGYPLUS
    assert validator.processor_name == "EnergyPlus Simulation"
    assert validator.has_processor is True
    assert validator.is_system is True

    # Check catalog entries were created
    slugs = set(validator.catalog_entries.values_list("slug", flat=True))
    assert "site_electricity_kwh" in slugs
    assert "heating_energy_kwh" in slugs
    assert "total_unmet_hours" in slugs  # derivation


@pytest.mark.django_db
def test_sync_system_validators_is_idempotent():
    """Running sync twice should not duplicate entries."""
    Validator.objects.filter(slug="energyplus-idf-validator").delete()

    call_command("sync_system_validators")
    initial_count = ValidatorCatalogEntry.objects.filter(
        validator__slug="energyplus-idf-validator"
    ).count()

    call_command("sync_system_validators")
    final_count = ValidatorCatalogEntry.objects.filter(
        validator__slug="energyplus-idf-validator"
    ).count()

    assert initial_count == final_count
    assert initial_count == len(ENERGYPLUS_SEED["catalog_entries"])
