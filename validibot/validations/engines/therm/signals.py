"""
Signal extraction from a parsed ThermModel.

Signals are key-value pairs that become:
1. The payload for this step's assertion evaluation
2. Available to downstream workflow steps via cross-step signal access

Signal values are read from the XML, not computed by a thermal solver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.engines.therm.geometry import all_polygons_closed
from validibot.validations.engines.therm.geometry import compute_bounding_box

if TYPE_CHECKING:
    from validibot.validations.engines.therm.models import ThermModel


def extract_signals(model: ThermModel) -> dict[str, Any]:
    """
    Extract all signals from a parsed ThermModel.

    Returns a flat dict of signal_slug -> value pairs matching the
    catalog entries defined in seeds/therm.py.
    """
    signals: dict[str, Any] = {}

    # Counts
    signals["polygon_count"] = len(model.polygons)
    signals["material_count"] = len(model.materials)
    signals["bc_count"] = len(model.boundary_conditions)

    # Geometry
    if model.polygons:
        width, height = compute_bounding_box(model.polygons)
        signals["geometry_width_mm"] = width
        signals["geometry_height_mm"] = height
    else:
        signals["geometry_width_mm"] = 0.0
        signals["geometry_height_mm"] = 0.0

    signals["all_polygons_closed"] = all_polygons_closed(model.polygons)

    # Boundary conditions - find interior and exterior
    interior_bc = _find_bc_by_type(model, "interior")
    exterior_bc = _find_bc_by_type(model, "exterior")

    if interior_bc:
        signals["interior_bc_temp"] = interior_bc.temperature
        signals["interior_film_coeff"] = interior_bc.film_coefficient
    else:
        signals["interior_bc_temp"] = None
        signals["interior_film_coeff"] = None

    if exterior_bc:
        signals["exterior_bc_temp"] = exterior_bc.temperature
        signals["exterior_film_coeff"] = exterior_bc.film_coefficient
    else:
        signals["exterior_bc_temp"] = None
        signals["exterior_film_coeff"] = None

    # U-factor tags
    signals["ufactor_tags_found"] = [t.name for t in model.ufactor_tags]

    # Mesh
    signals["mesh_level"] = model.mesh_params.mesh_level if model.mesh_params else None

    # Flags
    signals["has_cma_data"] = model.has_cma_data
    signals["has_glazing_system"] = model.has_glazing_system

    # Version
    signals["therm_version"] = model.therm_version

    return signals


def _find_bc_by_type(model: ThermModel, bc_type: str):
    """Find the first boundary condition matching the given type."""
    for bc in model.boundary_conditions.values():
        if bc.bc_type == bc_type:
            return bc
    return None
