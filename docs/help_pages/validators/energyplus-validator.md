# EnergyPlus Validator

The EnergyPlus Validator runs building energy simulations using the EnergyPlus engine and extracts performance metrics as signals for assertion evaluation.

This is a container-based (advanced) validator. It dispatches your submission to an isolated EnergyPlus container, runs the simulation, and collects the output metrics. You can then write CEL assertions against those metrics to check compliance with energy codes, design targets, or performance requirements.

---

## What it does

1. **Accepts** IDF or epJSON building energy model files
2. **Optionally resolves** template parameters (if using parametric submissions)
3. **Runs** the EnergyPlus simulation in an isolated container
4. **Extracts** energy consumption, comfort, and building metrics from the simulation output
5. **Exposes** all metrics as signals for CEL assertion evaluation

---

## Input signals

These are values you provide with your submission (as metadata). They're used for comparison assertions against simulation output.

| Signal | Type | Description |
|--------|------|-------------|
| `expected_floor_area_m2` | Number | Your expected floor area, for comparison with simulated value |
| `target_eui_kwh_m2` | Number | Target Energy Use Intensity for code compliance |
| `max_unmet_hours` | Number | Maximum allowable unmet heating/cooling hours |

---

## Output signals

These are extracted from the EnergyPlus simulation results. Use them in CEL assertions to check performance.

### Energy consumption

| Signal | Type | Description |
|--------|------|-------------|
| `site_electricity_kwh` | Number | Total site electricity consumption |
| `site_natural_gas_kwh` | Number | Total site natural gas consumption |
| `site_district_cooling_kwh` | Number | Total district cooling consumption |
| `site_district_heating_kwh` | Number | Total district heating consumption |

### Energy intensity

| Signal | Type | Description |
|--------|------|-------------|
| `site_eui_kwh_m2` | Number | Site energy use intensity (total energy per floor area) |

### End-use breakdown

| Signal | Type | Description |
|--------|------|-------------|
| `heating_energy_kwh` | Number | Total heating energy |
| `cooling_energy_kwh` | Number | Total cooling energy |
| `interior_lighting_kwh` | Number | Interior lighting energy |
| `fans_energy_kwh` | Number | Fan energy consumption |
| `pumps_energy_kwh` | Number | Pump energy consumption |
| `water_systems_kwh` | Number | Water systems energy |

### Comfort and performance

| Signal | Type | Description |
|--------|------|-------------|
| `unmet_heating_hours` | Number | Hours where heating setpoint was not met |
| `unmet_cooling_hours` | Number | Hours where cooling setpoint was not met |
| `peak_electric_demand_w` | Number | Peak electrical demand in watts |

### Building characteristics

| Signal | Type | Description |
|--------|------|-------------|
| `floor_area_m2` | Number | Total conditioned floor area from the model |
| `zone_count` | Number | Number of thermal zones in the model |

### Window and envelope

| Signal | Type | Description |
|--------|------|-------------|
| `window_heat_gain_kwh` | Number | Heat gain through windows |
| `window_heat_loss_kwh` | Number | Heat loss through windows |
| `window_transmitted_solar_kwh` | Number | Solar energy transmitted through windows |

### Derived signals

These are computed from other signals:

| Signal | Type | Computed from |
|--------|------|---------------|
| `total_unmet_hours` | Number | `unmet_heating_hours + unmet_cooling_hours` |
| `total_site_energy_kwh` | Number | Sum of all energy source signals |

---

## Example assertions

```
// EUI must be below the ASHRAE 90.1 target
site_eui_kwh_m2 <= target_eui_kwh_m2

// Total unmet hours must be within acceptable range
total_unmet_hours <= max_unmet_hours

// Floor area should match the expected value (within 1%)
floor_area_m2 >= expected_floor_area_m2 * 0.99
  && floor_area_m2 <= expected_floor_area_m2 * 1.01

// Peak demand must be below utility limit
peak_electric_demand_w < 500000.0
```

---

## File types

The EnergyPlus Validator accepts IDF (Input Data File) and epJSON files.

---

## Template support

The validator supports parametric submissions using JSON templates. You submit a template with placeholder parameters, and the validator resolves them before running the simulation. This is useful for parametric studies where you want to sweep through different building configurations.

---

## Tips

- **Check unmet hours** as a sanity check. High unmet hours mean the HVAC system couldn't maintain setpoints, which usually indicates an undersized system or a modeling error.
- **Compare floor area** against your expected value. A mismatch often means the model geometry was modified without updating the metadata.
- **Use derived signals** like `total_site_energy_kwh` for overall compliance checks rather than summing individual signals in your CEL expression.
