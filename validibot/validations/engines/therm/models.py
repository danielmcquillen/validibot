"""
Dataclasses for the parsed THERM model intermediate representation.

TODO: Field names and types should be determined from actual THERM
XML files and LBNL documentation. These are generic placeholders
representing the expected categories of data in a THERM model.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass
class ThermPolygon:
    """A single polygon in the THERM geometry."""

    id: str
    material_id: str
    vertices: list[tuple[float, float]]  # (x, y) coordinate pairs
    name: str | None = None


@dataclass
class ThermMaterial:
    """A material definition."""

    id: str
    name: str
    # TODO: add fields based on actual THERM XML schema


@dataclass
class ThermBoundaryCondition:
    """A boundary condition definition."""

    id: str
    name: str
    # TODO: add fields based on actual THERM XML schema


@dataclass
class ThermUFactorTag:
    """A U-factor calculation tag."""

    name: str
    tag_type: str


@dataclass
class ThermMeshParameters:
    """Mesh configuration."""

    # TODO: add fields based on actual THERM XML schema


@dataclass
class ThermModel:
    """
    Complete parsed representation of a THERM file.

    This is the intermediate representation that all domain checks
    and signal extraction operate on. It is format-agnostic — the
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
