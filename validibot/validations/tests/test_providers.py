"""
Tests for system validator configs and sync functionality.
"""

import pytest
from django.core.management import call_command

from validibot.validations.constants import ValidationType
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator


@pytest.mark.django_db
def test_sync_validators_creates_energyplus():
    """The sync command should create the EnergyPlus validator
    and signal definitions.
    """
    # Ensure we start clean
    Validator.objects.filter(slug="energyplus-idf-validator").delete()

    call_command("sync_validators")

    validator = Validator.objects.get(slug="energyplus-idf-validator")
    assert validator.validation_type == ValidationType.ENERGYPLUS
    assert validator.processor_name == "EnergyPlus™ Simulation"
    assert validator.has_processor is True
    assert validator.is_system is True

    # Check signal definitions were created
    keys = set(
        validator.signal_definitions.values_list("contract_key", flat=True),
    )
    assert "site_electricity_kwh" in keys
    assert "heating_energy_kwh" in keys


@pytest.mark.django_db
def test_sync_validators_is_idempotent():
    """Running sync twice should not duplicate entries."""
    Validator.objects.filter(slug="energyplus-idf-validator").delete()

    call_command("sync_validators")
    initial_count = SignalDefinition.objects.filter(
        validator__slug="energyplus-idf-validator",
    ).count()

    call_command("sync_validators")
    final_count = SignalDefinition.objects.filter(
        validator__slug="energyplus-idf-validator",
    ).count()

    assert initial_count == final_count
    assert initial_count > 0
