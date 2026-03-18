"""
Spacecraft Thermal Radiator Panel — Toy FMU for Validibot Demo

Models a spacecraft radiator panel's equilibrium temperature using
the Stefan-Boltzmann law. A simple but physically meaningful model
that's standard fare in space systems engineering courses.

Physics:
    Q_solar = absorptivity * panel_area * solar_irradiance
    Q_radiated = emissivity * stefan_boltzmann * panel_area * (T^4 - T_space^4)
    At equilibrium: Q_solar = Q_radiated, solve for T

Inputs:
    solar_irradiance  [W/m²]   Solar flux at spacecraft location (1361 at 1 AU)
    panel_area        [m²]     Radiator panel surface area
    emissivity        [-]      Thermal emissivity of radiator surface (0-1)
    absorptivity      [-]      Solar absorptivity of radiator surface (0-1)

Outputs:
    equilibrium_temp  [K]      Radiator equilibrium temperature
    heat_rejected     [W]      Total heat rejected at equilibrium
"""

from pythonfmu import Fmi2Causality
from pythonfmu import Fmi2Slave
from pythonfmu import Real

# Stefan-Boltzmann constant [W/(m²·K⁴)]
STEFAN_BOLTZMANN = 5.670374419e-8

# Deep space background temperature [K] (cosmic microwave background)
T_SPACE = 2.725


class ThermalRadiator(Fmi2Slave):
    """
    FMI 2.0 Co-Simulation slave modeling a spacecraft thermal radiator panel.
    """

    description = "Spacecraft thermal radiator panel equilibrium model"
    author = "Validibot Examples"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Inputs
        self.solar_irradiance = 1361.0  # W/m², default = 1 AU solar constant
        self.panel_area = 2.0  # m²
        self.emissivity = 0.85  # typical white paint radiator
        self.absorptivity = 0.20  # typical white paint radiator

        # Outputs
        self.equilibrium_temp = 0.0  # K
        self.heat_rejected = 0.0  # W

        # Register variables with FMI
        self.register_variable(Real("solar_irradiance", causality=Fmi2Causality.input))
        self.register_variable(Real("panel_area", causality=Fmi2Causality.input))
        self.register_variable(Real("emissivity", causality=Fmi2Causality.input))
        self.register_variable(Real("absorptivity", causality=Fmi2Causality.input))
        self.register_variable(Real("equilibrium_temp", causality=Fmi2Causality.output))
        self.register_variable(Real("heat_rejected", causality=Fmi2Causality.output))

    def do_step(self, current_time, step_size):
        """
        Compute radiator equilibrium temperature via Stefan-Boltzmann.

        At thermal equilibrium:
            α · A · S = ε · σ · A · (T⁴ − T_space⁴)

        Solving for T:
            T = ((α · S) / (ε · σ) + T_space⁴) ^ (1/4)
        """
        if self.emissivity <= 0.0 or self.panel_area <= 0.0:
            # Physically impossible — can't radiate with zero emissivity
            self.equilibrium_temp = float("nan")
            self.heat_rejected = float("nan")
            return True

        # Absorbed solar power per unit area
        q_solar = self.absorptivity * self.solar_irradiance

        # Solve Stefan-Boltzmann for equilibrium temperature
        t4 = (q_solar / (self.emissivity * STEFAN_BOLTZMANN)) + (T_SPACE**4)
        self.equilibrium_temp = t4**0.25

        # Total heat rejected at equilibrium
        self.heat_rejected = (
            self.emissivity
            * STEFAN_BOLTZMANN
            * self.panel_area
            * (self.equilibrium_temp**4 - T_SPACE**4)
        )

        return True
