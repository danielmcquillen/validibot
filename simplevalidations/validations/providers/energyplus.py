from __future__ import annotations

from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogRunStage
from simplevalidations.validations.constants import CatalogValueType
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.providers import BaseValidationProvider
from simplevalidations.validations.providers import register_provider
from simplevalidations.validations.providers.models import CatalogEntryDefinition


@register_provider(ValidationType.ENERGYPLUS)
class EnergyPlusProvider(BaseValidationProvider):
    """
    Provider defining the core EnergyPlus signals/derivations available
    to authors. This is a minimal catalog; additional entries can be layered
    on as we expand Meter coverage.
    """

    def get_catalog_defaults(self):
        return [
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.INPUT,
                slug="floor_area_m2",
                label="Floor Area (m²)",
                data_type=CatalogValueType.NUMBER,
                description="Gross floor area pulled from submission metadata.",
                binding_config={
                    "source": "submission.metadata",
                    "path": "floor_area_m2",
                },
                metadata={"units": "m²"},
                is_required=False,
                order=10,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="facility_electric_demand_w",
                label="Facility Electricity Demand (W)",
                data_type=CatalogValueType.TIMESERIES,
                description=(
                    "Series sourced from Facility:Electricity:Demand [W] meter."
                ),
                binding_config={
                    "source": "series",
                    "meter": "Facility:Electricity:Demand [W]",
                },
                metadata={"units": "W"},
                is_required=True,
                order=20,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="facility_electricity_kwh",
                label="Facility Electricity (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total site electricity consumption.",
                binding_config={
                    "source": "metric",
                    "key": "site_electricity_kwh",
                },
                metadata={"units": "kWh"},
                is_required=True,
                order=30,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.DERIVATION,
                run_stage=CatalogRunStage.OUTPUT,
                slug="peak_demand_w",
                label="Peak Facility Demand (W)",
                data_type=CatalogValueType.NUMBER,
                description="Peak demand derived from the facility demand series.",
                binding_config={
                    "expr": "max(series('facility_electric_demand_w'))",
                },
                metadata={"units": "W"},
                is_required=False,
                order=40,
            ),
        ]
