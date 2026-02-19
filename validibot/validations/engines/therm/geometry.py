"""
Geometry validation for THERM polygons.

Uses Shapely for computational geometry operations when available.
If Shapely is not installed, geometry checks are skipped with an
INFO-level message rather than failing the validation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from validibot.validations.constants import Severity
from validibot.validations.engines.base import ValidationIssue

if TYPE_CHECKING:
    from validibot.validations.engines.therm.models import ThermPolygon

logger = logging.getLogger(__name__)

# Tolerance for floating-point geometry comparisons (mm)
CLOSURE_TOLERANCE = 0.01

# Minimum vertices required for a valid polygon
MIN_POLYGON_VERTICES = 3

# Minimum polygons required for gap detection
MIN_POLYGONS_FOR_GAP_CHECK = 2

# Area tolerance for overlap detection (mm^2)
OVERLAP_AREA_TOLERANCE = 0.1

# Area tolerance for gap detection (mm^2)
GAP_AREA_TOLERANCE = 1.0


def check_polygon_closure(
    polygons: list[ThermPolygon],
) -> list[ValidationIssue]:
    """
    Verify every polygon is geometrically closed.

    A closed polygon's first vertex equals its last vertex (within
    floating-point tolerance). Unclosed polygons are the most common
    cause of THERM meshing failures.
    """
    issues: list[ValidationIssue] = []
    for poly in polygons:
        if len(poly.vertices) < MIN_POLYGON_VERTICES:
            issues.append(
                ValidationIssue(
                    path=f"Polygon[{poly.id}]",
                    message=(
                        f"Polygon '{poly.id}' has fewer than "
                        f"{MIN_POLYGON_VERTICES} vertices."
                    ),
                    severity=Severity.ERROR,
                ),
            )
            continue

        first = poly.vertices[0]
        last = poly.vertices[-1]
        dx = abs(first[0] - last[0])
        dy = abs(first[1] - last[1])
        if dx > CLOSURE_TOLERANCE or dy > CLOSURE_TOLERANCE:
            issues.append(
                ValidationIssue(
                    path=f"Polygon[{poly.id}]",
                    message=(
                        f"Polygon '{poly.id}' is not closed. "
                        f"First vertex ({first[0]:.2f}, {first[1]:.2f}) != "
                        f"last vertex ({last[0]:.2f}, {last[1]:.2f})."
                    ),
                    severity=Severity.ERROR,
                ),
            )
    return issues


def check_polygon_validity(
    polygons: list[ThermPolygon],
) -> list[ValidationIssue]:
    """
    Check polygons for self-intersection using Shapely's is_valid.

    Self-intersecting polygons cannot be meshed and will cause THERM
    to fail at simulation time. Skipped if Shapely is not installed.
    """
    try:
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely.validation import explain_validity
    except ImportError:
        logger.info(
            "Shapely not installed; skipping polygon validity checks. "
            "Install shapely for full geometry validation.",
        )
        return []

    issues: list[ValidationIssue] = []
    for poly in polygons:
        if len(poly.vertices) < MIN_POLYGON_VERTICES:
            continue  # Already caught by closure check
        try:
            sp = ShapelyPolygon(poly.vertices)
            if not sp.is_valid:
                reason = explain_validity(sp)
                issues.append(
                    ValidationIssue(
                        path=f"Polygon[{poly.id}]",
                        message=(f"Polygon '{poly.id}' has invalid geometry: {reason}"),
                        severity=Severity.ERROR,
                    ),
                )
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    path=f"Polygon[{poly.id}]",
                    message=(f"Could not validate polygon '{poly.id}' geometry: {exc}"),
                    severity=Severity.WARNING,
                ),
            )
    return issues


def check_overlaps(
    polygons: list[ThermPolygon],
) -> list[ValidationIssue]:
    """
    Detect overlapping polygons.

    Two material regions should not occupy the same physical space.
    Uses Shapely intersection area > tolerance as the detection criterion.
    Skipped if Shapely is not installed.
    """
    try:
        from shapely.geometry import Polygon as ShapelyPolygon
    except ImportError:
        return []

    issues: list[ValidationIssue] = []

    # Build Shapely polygons, skipping those with too few vertices
    shapely_polys: list[tuple[str, ShapelyPolygon]] = []
    for poly in polygons:
        if len(poly.vertices) >= MIN_POLYGON_VERTICES:
            try:
                sp = ShapelyPolygon(poly.vertices)
                if sp.is_valid and sp.area > 0:
                    shapely_polys.append((poly.id, sp))
            except Exception:
                logger.debug("Skipping polygon %s for overlap check", poly.id)

    # Check all pairs for overlap
    for i in range(len(shapely_polys)):
        for j in range(i + 1, len(shapely_polys)):
            id_a, sp_a = shapely_polys[i]
            id_b, sp_b = shapely_polys[j]
            try:
                intersection = sp_a.intersection(sp_b)
                if intersection.area > OVERLAP_AREA_TOLERANCE:
                    issues.append(
                        ValidationIssue(
                            path=f"Polygon[{id_a}]",
                            message=(
                                f"Polygons '{id_a}' and '{id_b}' overlap by "
                                f"{intersection.area:.1f} mm^2."
                            ),
                            severity=Severity.ERROR,
                        ),
                    )
            except Exception:
                logger.debug("Overlap check failed for %s and %s", id_a, id_b)

    return issues


def check_gaps(
    polygons: list[ThermPolygon],
) -> list[ValidationIssue]:
    """
    Detect gaps between adjacent polygons.

    Uses the convex hull of all polygons minus the union of all polygons
    to find unmodeled regions. Gaps may be intentional for some
    configurations so these are reported as warnings.
    Skipped if Shapely is not installed.
    """
    try:
        from shapely.geometry import Polygon as ShapelyPolygon
        from shapely.ops import unary_union
    except ImportError:
        return []

    issues: list[ValidationIssue] = []

    shapely_polys: list[ShapelyPolygon] = []
    for poly in polygons:
        if len(poly.vertices) >= MIN_POLYGON_VERTICES:
            try:
                sp = ShapelyPolygon(poly.vertices)
                if sp.is_valid and sp.area > 0:
                    shapely_polys.append(sp)
            except Exception:
                logger.debug("Skipping polygon for gap check")

    if len(shapely_polys) < MIN_POLYGONS_FOR_GAP_CHECK:
        return issues

    try:
        union = unary_union(shapely_polys)
        hull = union.convex_hull
        gap_region = hull.difference(union)
        if gap_region.area > GAP_AREA_TOLERANCE:
            issues.append(
                ValidationIssue(
                    path="Geometry",
                    message=(
                        f"Gaps detected in geometry: {gap_region.area:.1f} mm^2 "
                        f"of unmodeled area within the convex hull of all polygons."
                    ),
                    severity=Severity.WARNING,
                ),
            )
    except Exception as exc:
        logger.debug("Gap detection failed: %s", exc)

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


def all_polygons_closed(polygons: list[ThermPolygon]) -> bool:
    """Return True if every polygon is geometrically closed."""
    for poly in polygons:
        if len(poly.vertices) < MIN_POLYGON_VERTICES:
            return False
        first = poly.vertices[0]
        last = poly.vertices[-1]
        dx = abs(first[0] - last[0])
        dy = abs(first[1] - last[1])
        if dx > CLOSURE_TOLERANCE or dy > CLOSURE_TOLERANCE:
            return False
    return True
