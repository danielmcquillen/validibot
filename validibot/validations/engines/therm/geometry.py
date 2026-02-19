"""
Geometry validation for THERM polygons.

TODO: Implement domain checks based on original research — creating
test THERM files with known geometry defects, running them through
THERM, and cataloging which malformations cause failures vs. silent
incorrect results vs. are handled gracefully.

Planned check categories:
- Polygon closure (first vertex == last vertex)
- Polygon self-intersection
- Polygon overlap detection
- Gap detection between adjacent polygons
- Bounding box computation for signal extraction
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.validations.engines.base import ValidationIssue
    from validibot.validations.engines.therm.models import ThermPolygon


def run_geometry_checks(
    polygons: list[ThermPolygon],
) -> list[ValidationIssue]:
    """
    Run all geometry checks against parsed polygons.

    Returns a list of ValidationIssue objects for any problems found.

    TODO: Implement specific checks. Each check should be developed
    by testing actual THERM files and documenting the failure modes.
    """
    issues: list[ValidationIssue] = []
    # TODO: implement geometry checks
    return issues


def compute_bounding_box(
    polygons: list[ThermPolygon],
) -> tuple[float, float]:
    """
    Compute the bounding box dimensions (width_mm, height_mm) of
    the complete geometry.

    Returns (0.0, 0.0) if there are no vertices.
    """
    all_x: list[float] = []
    all_y: list[float] = []
    for poly in polygons:
        for x, y in poly.vertices:
            all_x.append(x)
            all_y.append(y)

    if not all_x:
        return (0.0, 0.0)

    return (max(all_x) - min(all_x), max(all_y) - min(all_y))
