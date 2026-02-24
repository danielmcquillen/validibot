"""
Seed data for system validators with catalog entries.

This module contains the canonical definitions for validators
(EnergyPlus, FMU, THERM, etc.) and their catalog entries.

These are synced to the database via:
    python manage.py sync_advanced_validators
"""

from validibot.validations.seeds.energyplus import ENERGYPLUS_SEED
from validibot.validations.seeds.fmu import FMU_SEED
from validibot.validations.seeds.therm import THERM_SEED

# All system validator seeds
SYSTEM_VALIDATOR_SEEDS = [
    ENERGYPLUS_SEED,
    FMU_SEED,
    THERM_SEED,
]

__all__ = [
    "ENERGYPLUS_SEED",
    "FMU_SEED",
    "SYSTEM_VALIDATOR_SEEDS",
    "THERM_SEED",
]
