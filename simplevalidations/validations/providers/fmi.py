from __future__ import annotations

from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.providers import BaseValidationProvider
from simplevalidations.validations.providers import register_provider


@register_provider(ValidationType.FMI)
class FMIProvider(BaseValidationProvider):
    """
    Minimal provider stub for FMI validators.

    This provider defers most catalog population to the FMU introspection
    pipeline, which will attach FMIVariable rows and create catalog entries
    for selected inputs/outputs. The stub keeps the provider contract wired
    so engines can resolve a provider without failing.
    """

    def get_catalog_defaults(self):
        # FMI catalog entries are materialised dynamically from the attached FMU.
        return []
