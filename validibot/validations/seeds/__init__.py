"""
Seed data for advanced validators.

This module contains the canonical definitions for advanced validators
(EnergyPlus, FMI, etc.) and their catalog entries.

These are synced to the database via:
    python manage.py sync_advanced_validators
"""

from validibot.validations.seeds.energyplus import ENERGYPLUS_SEED
from validibot.validations.seeds.fmi import FMI_SEED

# All system validator seeds
SYSTEM_VALIDATOR_SEEDS = [
    ENERGYPLUS_SEED,
    FMI_SEED,
]

__all__ = [
    "ENERGYPLUS_SEED",
    "FMI_SEED",
    "SYSTEM_VALIDATOR_SEEDS",
]
