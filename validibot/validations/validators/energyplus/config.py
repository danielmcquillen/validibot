"""
Configuration for the EnergyPlus system validator.

The catalog entry binding_config["key"] values must match field names in
validibot_shared.energyplus.models.EnergyPlusSimulationMetrics, which is what
the container validator populates after running the simulation.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="energyplus-idf-validator",
    name="EnergyPlus Validator",
    description="Validate EnergyPlus IDF models and run simulations.",
    validation_type=ValidationType.ENERGYPLUS,
    version="1.0",
    order=10,
    has_processor=True,
    processor_name="EnergyPlus Simulation",
    is_system=True,
    supported_file_types=[SubmissionFileType.TEXT, SubmissionFileType.JSON],
    supported_data_formats=[
        SubmissionDataFormat.ENERGYPLUS_IDF,
        SubmissionDataFormat.ENERGYPLUS_EPJSON,
    ],
    allowed_extensions=["idf", "epjson", "json"],
    resource_types=[ResourceFileType.ENERGYPLUS_WEATHER],
    icon="bi-lightning-charge-fill",
    card_image="ENERGYPLUS_card_img_small.png",
    # Note: These signals are all prototypes and subject to changg. I need
    # to do more work to determine exactly which input and output signals
    # would make sense for a generic EnergyPlus simulation.
    catalog_entries=[
        # ==================================================================
        # INPUT SIGNALS (from submission metadata)
        # ==================================================================
        CatalogEntrySpec(
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
        CatalogEntrySpec(
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
        CatalogEntrySpec(
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
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_electricity_kwh",
            label="Site Electricity (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total site electricity consumption from simulation.",
            binding_config={"source": "metric", "key": "site_electricity_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=100,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_natural_gas_kwh",
            label="Site Natural Gas (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total site natural gas consumption from simulation.",
            binding_config={"source": "metric", "key": "site_natural_gas_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=101,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_district_cooling_kwh",
            label="Site District Cooling (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total district cooling energy (if present in model).",
            binding_config={"source": "metric", "key": "site_district_cooling_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=102,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_district_heating_kwh",
            label="Site District Heating (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total district heating energy (if present in model).",
            binding_config={"source": "metric", "key": "site_district_heating_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=103,
        ),
        # ==================================================================
        # OUTPUT SIGNALS - Energy Use Intensity
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_eui_kwh_m2",
            label="Site EUI (kWh/m²)",
            data_type=CatalogValueType.NUMBER,
            description="Site Energy Use Intensity (total energy / floor area).",
            binding_config={"source": "metric", "key": "site_eui_kwh_m2"},
            metadata={"units": "kWh/m²"},
            is_required=False,
            order=110,
        ),
        # ==================================================================
        # OUTPUT SIGNALS - End-Use Breakdown
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="heating_energy_kwh",
            label="Heating Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total space heating energy across all fuel types.",
            binding_config={"source": "metric", "key": "heating_energy_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=120,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="cooling_energy_kwh",
            label="Cooling Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total space cooling energy.",
            binding_config={"source": "metric", "key": "cooling_energy_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=121,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="interior_lighting_kwh",
            label="Interior Lighting (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total interior lighting energy.",
            binding_config={"source": "metric", "key": "interior_lighting_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=122,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="fans_energy_kwh",
            label="Fans Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total fan energy (supply, return, exhaust fans).",
            binding_config={"source": "metric", "key": "fans_energy_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=123,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="pumps_energy_kwh",
            label="Pumps Energy (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total pump energy (chilled water, hot water, condenser).",
            binding_config={"source": "metric", "key": "pumps_energy_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=124,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="water_systems_kwh",
            label="Water Systems (kWh)",
            data_type=CatalogValueType.NUMBER,
            description="Total domestic hot water energy.",
            binding_config={"source": "metric", "key": "water_systems_kwh"},
            metadata={"units": "kWh"},
            is_required=False,
            order=125,
        ),
        # ==================================================================
        # OUTPUT SIGNALS - Comfort / Performance
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="unmet_heating_hours",
            label="Unmet Heating Hours",
            data_type=CatalogValueType.NUMBER,
            description="Hours when heating setpoint was not met.",
            binding_config={"source": "metric", "key": "unmet_heating_hours"},
            metadata={"units": "hours"},
            is_required=False,
            order=130,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="unmet_cooling_hours",
            label="Unmet Cooling Hours",
            data_type=CatalogValueType.NUMBER,
            description="Hours when cooling setpoint was not met.",
            binding_config={"source": "metric", "key": "unmet_cooling_hours"},
            metadata={"units": "hours"},
            is_required=False,
            order=131,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="peak_electric_demand_w",
            label="Peak Electric Demand (W)",
            data_type=CatalogValueType.NUMBER,
            description="Peak electric demand during simulation.",
            binding_config={"source": "metric", "key": "peak_electric_demand_w"},
            metadata={"units": "W"},
            is_required=False,
            order=132,
        ),
        # ==================================================================
        # OUTPUT SIGNALS - Building Characteristics
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="floor_area_m2",
            label="Floor Area (m²)",
            data_type=CatalogValueType.NUMBER,
            description="Total conditioned floor area from simulation.",
            binding_config={"source": "metric", "key": "floor_area_m2"},
            metadata={"units": "m²"},
            is_required=False,
            order=140,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="zone_count",
            label="Zone Count",
            data_type=CatalogValueType.NUMBER,
            description="Number of thermal zones in the model.",
            binding_config={"source": "metric", "key": "zone_count"},
            metadata={"units": "count"},
            is_required=False,
            order=141,
        ),
        # ==================================================================
        # DERIVATIONS (computed from other signals)
        # ==================================================================
        CatalogEntrySpec(
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
        CatalogEntrySpec(
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
    ],
)
