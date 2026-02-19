"""Domain constants for THERM material property validation."""

# Thermal conductivity range (W/m-K)
CONDUCTIVITY_MIN = 0.01  # Aerogel insulation
CONDUCTIVITY_MAX = 500.0  # Copper

# Emissivity range (dimensionless, 0-1)
EMISSIVITY_MIN = 0.0
EMISSIVITY_MAX = 1.0

# Mesh level range (THERM uses integer levels)
MESH_LEVEL_MIN = 0
MESH_LEVEL_MAX = 12
