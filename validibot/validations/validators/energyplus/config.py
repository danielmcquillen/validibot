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
from validibot.validations.constants import ComputeTier
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="energyplus-idf-validator",
    name="EnergyPlus\u2122 Validator",
    short_description="Validate EnergyPlus IDF files and outputs.",
    description="Validate EnergyPlus\u2122 IDF models and run simulations.",
    validation_type=ValidationType.ENERGYPLUS,
    validator_class=(
        "validibot.validations.validators.energyplus.validator.EnergyPlusValidator"
    ),
    output_envelope_class=(
        "validibot_shared.energyplus.envelopes.EnergyPlusOutputEnvelope"
    ),
    image_name="validibot-validator-backend-energyplus",
    # Version bump to 1.1 per ADR-2026-05-22: catalog cleanup removes
    # three misconceived "expectation" inputs (expected_floor_area_m2,
    # target_eui_kwh_m2, max_unmet_hours), adds three parser-extracted
    # step inputs (idf_version, zone_count, north_axis_deg), and
    # removes the redundant output zone_count (parsed-from-IDF facts
    # are step inputs, never step outputs).
    #
    # NOTE on floor_area_m2: ADR-2026-05-22 also proposed renaming the
    # simulation-derived output floor_area_m2 → simulated_conditioned_area_m2
    # for provenance clarity. That rename was DEFERRED — it requires a
    # coordinated validibot-shared package release (the Pydantic model
    # field is in the published package). The catalog still declares
    # floor_area_m2 so the slug matches the value the container emits.
    # Re-apply the rename in a follow-up PR once validibot-shared ships
    # the renamed field.
    #
    # sync_validators refuses to apply semantic drift under the same
    # (slug, version) so the bump is required.
    version="1.1",
    order=10,
    has_processor=True,
    processor_name="EnergyPlus\u2122 Simulation",
    is_system=True,
    supports_assertions=True,
    compute_tier=ComputeTier.HIGH,
    supported_file_types=[SubmissionFileType.TEXT, SubmissionFileType.JSON],
    supported_data_formats=[
        SubmissionDataFormat.ENERGYPLUS_IDF,
        SubmissionDataFormat.ENERGYPLUS_EPJSON,
    ],
    allowed_extensions=["idf", "epjson", "json"],
    resource_types=[ResourceFileType.ENERGYPLUS_WEATHER],
    icon="bi-lightning-charge-fill",
    card_image="ENERGYPLUS_card_img_small.png",
    catalog_entries=[
        # ==================================================================
        # STEP INPUTS \u2014 parser-extracted facts from the (resolved) IDF.
        #
        # Per ADR-2026-05-22, EnergyPlus is a Position 3 validator (process
        # has discrete input and output stages). These three step inputs are
        # the proof-of-concept set scaling to ~12 in Phase 2:
        #   - idf_version    (string,  always present, on_missing=error)
        #   - zone_count     (int,     always \u22651,     on_missing=error)
        #   - north_axis_deg (number,  defaults 0.0,  on_missing=null)
        #
        # Populated by EnergyPlusValidator.extract_input_signals() running
        # after preprocess_submission() \u2014 works for both direct-IDF and
        # template-mode submissions because preprocessing has resolved any
        # template variables into a concrete IDF by then.
        #
        # Authors reference these as i.idf_version, i.zone_count,
        # i.north_axis_deg in input-stage CEL assertions.
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.INPUT,
            slug="idf_version",
            label="IDF Version",
            data_type=CatalogValueType.STRING,
            description=(
                "EnergyPlus version declared by the IDF Version object "
                "(e.g. '25.1'). Every valid IDF has a Version object; "
                "absence indicates the file is malformed."
            ),
            binding_config={"source": "parser", "key": "idf_version"},
            metadata={},
            is_required=True,
            on_missing="error",  # every valid IDF has a Version object
            order=10,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.INPUT,
            slug="zone_count",
            label="Zone Count",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Count of Zone objects in the IDF. Must be at least 1 "
                "for a meaningful model. Useful for 'must have \u2265N zones' "
                "assertions before paying for simulation."
            ),
            binding_config={"source": "parser", "key": "zone_count"},
            metadata={"units": "count"},
            is_required=True,
            on_missing="error",  # absence means parsing failed
            order=11,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.INPUT,
            slug="north_axis_deg",
            label="North Axis (deg)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Building rotation in degrees, from the Building object "
                "North Axis field. Defaults to 0.0 per EnergyPlus IDD. "
                "Useful for orientation-sensitivity assertions."
            ),
            binding_config={"source": "parser", "key": "north_axis_deg"},
            metadata={"units": "deg"},
            is_required=False,
            on_missing="null",  # fall back to EnergyPlus default 0.0
            order=12,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # OUTPUT SIGNALS - Energy Use Intensity
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="site_eui_kwh_m2",
            label="Site EUI (kWh/m\u00b2)",
            data_type=CatalogValueType.NUMBER,
            description="Site Energy Use Intensity (total energy / floor area).",
            binding_config={"source": "metric", "key": "site_eui_kwh_m2"},
            metadata={"units": "kWh/m\u00b2"},
            is_required=False,
            order=110,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # OUTPUT SIGNALS - Building Characteristics (simulation-derived)
        #
        # Per ADR-2026-05-22's provenance rule: anything derived from the
        # IDF text is a step input (i.*); anything derived from EnergyPlus
        # simulation output is a step output (o.*). The output zone_count
        # has been removed \u2014 i.zone_count is the single source going
        # forward.
        #
        # NOTE on floor_area_m2: ADR-2026-05-22 also proposed renaming
        # this to simulated_conditioned_area_m2 for provenance clarity.
        # That rename requires a coordinated validibot-shared package
        # release (the Pydantic model field is in the published package
        # per the project's PyPI dependency policy). Until the shared
        # package ships the renamed field, the catalog continues to
        # declare floor_area_m2 so the slug matches the runtime value
        # the container actually produces. The rename is tracked as
        # follow-up work in the ADR's "deferred" section.
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="floor_area_m2",
            label="Floor Area (m\u00b2)",
            data_type=CatalogValueType.NUMBER,
            description="Total conditioned floor area from simulation.",
            binding_config={"source": "metric", "key": "floor_area_m2"},
            metadata={"units": "m\u00b2"},
            is_required=False,
            order=140,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        # ==================================================================
        # OUTPUT SIGNALS - Window Envelope
        # ==================================================================
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="window_heat_gain_kwh",
            label="Window Heat Gain (kWh)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Total annual heat gain through windows. Extracted from "
                "Surface Window Heat Gain Energy output variable."
            ),
            binding_config={"source": "metric", "key": "window_heat_gain_kwh"},
            metadata={"units": "kWh", "precision": 1},
            is_required=False,
            order=150,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="window_heat_loss_kwh",
            label="Window Heat Loss (kWh)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Total annual heat loss through windows. Extracted from "
                "Surface Window Heat Loss Energy output variable."
            ),
            binding_config={"source": "metric", "key": "window_heat_loss_kwh"},
            metadata={"units": "kWh", "precision": 1},
            is_required=False,
            order=151,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        ),
        CatalogEntrySpec(
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=CatalogRunStage.OUTPUT,
            slug="window_transmitted_solar_kwh",
            label="Transmitted Solar (kWh)",
            data_type=CatalogValueType.NUMBER,
            description=(
                "Total annual solar radiation transmitted through windows. "
                "Direct expression of SHGC effect. Extracted from Surface "
                "Window Transmitted Solar Radiation Energy output variable."
            ),
            binding_config={
                "source": "metric",
                "key": "window_transmitted_solar_kwh",
            },
            metadata={"units": "kWh", "precision": 1},
            is_required=False,
            order=152,
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
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
    # Template variable editing is handled by the unified signals card \u2014
    # no custom step_editor_cards needed.
)
