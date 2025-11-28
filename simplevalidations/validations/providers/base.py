from __future__ import annotations

from simplevalidations.validations.cel import CelHelper
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import Validator
from simplevalidations.validations.models import ValidatorCatalogEntry
from simplevalidations.validations.providers.models import CatalogEntryDefinition


class BaseValidationProvider:
    """
    Base class for validator-specific providers.
    Providers own catalog defaults, helper metadata, and preflight hooks.
    """

    validation_type: ValidationType

    def __init__(self, validator: Validator):
        self.validator = validator

    # ------------------------------------------------------------------
    # Catalog management
    # ------------------------------------------------------------------

    def get_catalog_defaults(self) -> list[CatalogEntryDefinition]:
        """
        Returns catalog entry definitions for this provider.
        Each dict should include entry_type, slug, label, data_type, description,
        binding_config, metadata, is_required, and order keys.
        """
        return []

    def ensure_catalog_entries(self) -> tuple[int, int]:
        """
        Ensure that all catalog defaults exist for the validator.
        Returns (created_count, existing_count).
        """
        if not self.validator.is_system:
            return (0, 0)

        created = 0
        existing = 0
        defaults = self.get_catalog_defaults()
        for entry in defaults:
            data = entry.model_dump()
            entry_type = data.pop("entry_type")
            slug = data.pop("slug")
            obj, was_created = ValidatorCatalogEntry.objects.get_or_create(
                validator=self.validator,
                entry_type=entry_type,
                slug=slug,
                defaults=data,
            )
            if was_created:
                created += 1
            else:
                existing += 1
        return created, existing

    # ------------------------------------------------------------------
    # Helper metadata
    # ------------------------------------------------------------------

    def get_helper_overrides(self) -> dict[str, CelHelper]:
        """
        Providers can expose additional CEL helpers or override documentation.
        """
        return {}

    # ------------------------------------------------------------------
    # Runtime hooks (placeholders for future phases)
    # ------------------------------------------------------------------

    def preflight_validate(self, ruleset, catalog):
        """Hook for validating ruleset catalog references."""
        return None

    def instrument(self, configuration):
        """Hook for provider-specific instrumentation (EnergyPlus, etc.)."""
        return configuration

    def bind(self, run_context):
        """Hook for per-run binding injection."""
        return {}
