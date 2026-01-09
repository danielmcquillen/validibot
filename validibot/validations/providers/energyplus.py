from __future__ import annotations

from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ValidationType
from validibot.validations.providers import BaseValidationProvider
from validibot.validations.providers import register_provider
from validibot.validations.providers.models import CatalogEntryDefinition


@register_provider(ValidationType.ENERGYPLUS)
class EnergyPlusProvider(BaseValidationProvider):
    """
    Provider defining the core EnergyPlus signals available to workflow authors.

    Output signals are extracted from EnergyPlus simulation results (eplusout.sql)
    and made available for assertions. The binding_config["key"] values must match
    field names in vb_shared.energyplus.models.EnergyPlusSimulationMetrics.

    Input signals can be bound from submission metadata for use in assertions
    (e.g., comparing simulated floor area against submitted floor area).
    """

    def get_catalog_defaults(self):
        return [
            # ==================================================================
            # INPUT SIGNALS (from submission metadata)
            # ==================================================================
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.INPUT,
                slug="expected_floor_area_m2",
                label="Expected Floor Area (m²)",
                data_type=CatalogValueType.NUMBER,
                description=(
                    "User-provided expected floor area from submission metadata. "
                    "Can be compared against simulated floor_area_m2."
                ),
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
                run_stage=CatalogRunStage.INPUT,
                slug="target_eui_kwh_m2",
                label="Target EUI (kWh/m²)",
                data_type=CatalogValueType.NUMBER,
                description=(
                    "Target Energy Use Intensity from submission metadata. "
                    "Used for compliance checking against simulated EUI."
                ),
                binding_config={
                    "source": "submission.metadata",
                    "path": "target_eui_kwh_m2",
                },
                metadata={"units": "kWh/m²"},
                is_required=False,
                order=11,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.INPUT,
                slug="max_unmet_hours",
                label="Max Unmet Hours",
                data_type=CatalogValueType.NUMBER,
                description=(
                    "Maximum allowable unmet heating/cooling hours. "
                    "Used for comfort compliance checking."
                ),
                binding_config={
                    "source": "submission.metadata",
                    "path": "max_unmet_hours",
                },
                metadata={"units": "hours"},
                is_required=False,
                order=12,
            ),
            # ==================================================================
            # OUTPUT SIGNALS - Energy Consumption
            # ==================================================================
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="site_electricity_kwh",
                label="Site Electricity (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total site electricity consumption from simulation.",
                binding_config={
                    "source": "metric",
                    "key": "site_electricity_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=100,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="site_natural_gas_kwh",
                label="Site Natural Gas (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total site natural gas consumption from simulation.",
                binding_config={
                    "source": "metric",
                    "key": "site_natural_gas_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=101,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="site_district_cooling_kwh",
                label="Site District Cooling (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total district cooling energy (if present in model).",
                binding_config={
                    "source": "metric",
                    "key": "site_district_cooling_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=102,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="site_district_heating_kwh",
                label="Site District Heating (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total district heating energy (if present in model).",
                binding_config={
                    "source": "metric",
                    "key": "site_district_heating_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=103,
            ),
            # ==================================================================
            # OUTPUT SIGNALS - Energy Use Intensity
            # ==================================================================
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="site_eui_kwh_m2",
                label="Site EUI (kWh/m²)",
                data_type=CatalogValueType.NUMBER,
                description=(
                    "Site Energy Use Intensity (total energy / floor area)."
                ),
                binding_config={
                    "source": "metric",
                    "key": "site_eui_kwh_m2",
                },
                metadata={"units": "kWh/m²"},
                is_required=False,
                order=110,
            ),
            # ==================================================================
            # OUTPUT SIGNALS - End-Use Breakdown
            # ==================================================================
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="heating_energy_kwh",
                label="Heating Energy (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total space heating energy across all fuel types.",
                binding_config={
                    "source": "metric",
                    "key": "heating_energy_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=120,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="cooling_energy_kwh",
                label="Cooling Energy (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total space cooling energy.",
                binding_config={
                    "source": "metric",
                    "key": "cooling_energy_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=121,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="interior_lighting_kwh",
                label="Interior Lighting (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total interior lighting energy.",
                binding_config={
                    "source": "metric",
                    "key": "interior_lighting_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=122,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="fans_energy_kwh",
                label="Fans Energy (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total fan energy (supply, return, exhaust fans).",
                binding_config={
                    "source": "metric",
                    "key": "fans_energy_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=123,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="pumps_energy_kwh",
                label="Pumps Energy (kWh)",
                data_type=CatalogValueType.NUMBER,
                description=(
                    "Total pump energy (chilled water, hot water, condenser)."
                ),
                binding_config={
                    "source": "metric",
                    "key": "pumps_energy_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=124,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="water_systems_kwh",
                label="Water Systems (kWh)",
                data_type=CatalogValueType.NUMBER,
                description="Total domestic hot water energy.",
                binding_config={
                    "source": "metric",
                    "key": "water_systems_kwh",
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=125,
            ),
            # ==================================================================
            # OUTPUT SIGNALS - Comfort / Performance
            # ==================================================================
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="unmet_heating_hours",
                label="Unmet Heating Hours",
                data_type=CatalogValueType.NUMBER,
                description="Hours when heating setpoint was not met.",
                binding_config={
                    "source": "metric",
                    "key": "unmet_heating_hours",
                },
                metadata={"units": "hours"},
                is_required=False,
                order=130,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="unmet_cooling_hours",
                label="Unmet Cooling Hours",
                data_type=CatalogValueType.NUMBER,
                description="Hours when cooling setpoint was not met.",
                binding_config={
                    "source": "metric",
                    "key": "unmet_cooling_hours",
                },
                metadata={"units": "hours"},
                is_required=False,
                order=131,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="peak_electric_demand_w",
                label="Peak Electric Demand (W)",
                data_type=CatalogValueType.NUMBER,
                description="Peak electric demand during simulation.",
                binding_config={
                    "source": "metric",
                    "key": "peak_electric_demand_w",
                },
                metadata={"units": "W"},
                is_required=False,
                order=132,
            ),
            # ==================================================================
            # OUTPUT SIGNALS - Building Characteristics
            # ==================================================================
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="floor_area_m2",
                label="Floor Area (m²)",
                data_type=CatalogValueType.NUMBER,
                description="Total conditioned floor area from simulation.",
                binding_config={
                    "source": "metric",
                    "key": "floor_area_m2",
                },
                metadata={"units": "m²"},
                is_required=False,
                order=140,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.SIGNAL,
                run_stage=CatalogRunStage.OUTPUT,
                slug="zone_count",
                label="Zone Count",
                data_type=CatalogValueType.NUMBER,
                description="Number of thermal zones in the model.",
                binding_config={
                    "source": "metric",
                    "key": "zone_count",
                },
                metadata={"units": "count"},
                is_required=False,
                order=141,
            ),
            # ==================================================================
            # DERIVATIONS (computed from other signals)
            # ==================================================================
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.DERIVATION,
                run_stage=CatalogRunStage.OUTPUT,
                slug="total_unmet_hours",
                label="Total Unmet Hours",
                data_type=CatalogValueType.NUMBER,
                description=(
                    "Combined unmet heating and cooling hours. "
                    "Derived from unmet_heating_hours + unmet_cooling_hours."
                ),
                binding_config={
                    "expr": "unmet_heating_hours + unmet_cooling_hours",
                },
                metadata={"units": "hours"},
                is_required=False,
                order=200,
            ),
            CatalogEntryDefinition(
                entry_type=CatalogEntryType.DERIVATION,
                run_stage=CatalogRunStage.OUTPUT,
                slug="total_site_energy_kwh",
                label="Total Site Energy (kWh)",
                data_type=CatalogValueType.NUMBER,
                description=(
                    "Total site energy consumption (electricity + gas + district)."
                ),
                binding_config={
                    "expr": (
                        "(site_electricity_kwh ?? 0) + "
                        "(site_natural_gas_kwh ?? 0) + "
                        "(site_district_cooling_kwh ?? 0) + "
                        "(site_district_heating_kwh ?? 0)"
                    ),
                },
                metadata={"units": "kWh"},
                is_required=False,
                order=201,
            ),
        ]
