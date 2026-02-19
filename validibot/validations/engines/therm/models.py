"""Dataclasses for the parsed THERM model intermediate representation."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass
class ThermPolygon:
    """A single polygon in the THERM geometry."""

    id: str
    material_id: str
    vertices: list[tuple[float, float]]  # (x, y) in mm
    name: str | None = None


@dataclass
class ThermMaterial:
    """A material definition."""

    id: str
    name: str
    material_type: str  # "solid", "frame cavity", etc.
    conductivity: float | None  # W/m-K
    emissivity_inside: float | None = None  # 0-1
    emissivity_outside: float | None = None  # 0-1


@dataclass
class ThermBoundaryCondition:
    """A boundary condition assignment."""

    id: str
    name: str
    bc_type: str  # "interior", "exterior", "adiabatic", etc.
    temperature: float | None = None  # degrees C
    film_coefficient: float | None = None  # W/m2-K
    radiation_model: str | None = None


@dataclass
class ThermUFactorTag:
    """A U-factor calculation tag."""

    name: str  # "Frame", "Edge", "Center", "Divider", etc.
    tag_type: str


@dataclass
class ThermMeshParameters:
    """Mesh configuration."""

    mesh_level: int | None = None  # Typically 0-12
    error_limit: float | None = None


@dataclass
class ThermModel:
    """
    Complete parsed representation of a THERM file.

    This is the intermediate representation that all domain checks
    and signal extraction operate on. It is format-agnostic - the
    parser produces the same ThermModel whether the input was .thmx
    or .thmz.
    """

    # File metadata
    source_format: str  # "thmx" or "thmz"
    therm_version: str | None

    # Core data
    polygons: list[ThermPolygon] = field(default_factory=list)
    materials: dict[str, ThermMaterial] = field(default_factory=dict)
    boundary_conditions: dict[str, ThermBoundaryCondition] = field(default_factory=dict)
    ufactor_tags: list[ThermUFactorTag] = field(default_factory=list)
    mesh_params: ThermMeshParameters | None = None

    # Flags
    has_cma_data: bool = False
    has_glazing_system: bool = False

    # Raw XML root for advanced queries (not serialized)
    xml_root: Any = field(default=None, repr=False)
