"""
Seed data for the FMU system validator.

FMU validators have their catalog entries created dynamically from
the attached FMU via introspection, so this seed only defines the
validator metadata itself.
"""

from validibot.validations.constants import ValidationType

FMI_SEED = {
    "validator": {
        "slug": "fmi-fmu-validator",
        "name": "FMI/FMU Validation",
        "description": "Validate and simulate Functional Mock-up Units (FMUs).",
        "validation_type": ValidationType.FMU,
        "version": "1.0",
        "order": 20,
        "has_processor": True,
        "processor_name": "FMU Simulation",
        "is_system": True,
    },
    # FMU catalog entries are materialized dynamically from the attached FMU
    # via introspection, so we don't define any static entries here.
    "catalog_entries": [],
}
