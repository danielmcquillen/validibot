"""Boundary condition and reference integrity validation for THERM models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from validibot.validations.constants import Severity
from validibot.validations.engines.base import ValidationIssue

if TYPE_CHECKING:
    from validibot.validations.engines.therm.models import ThermBoundaryCondition
    from validibot.validations.engines.therm.models import ThermMaterial
    from validibot.validations.engines.therm.models import ThermPolygon


def check_reference_integrity(
    polygons: list[ThermPolygon],
    materials: dict[str, ThermMaterial],
    boundary_conditions: dict[str, ThermBoundaryCondition],
) -> list[ValidationIssue]:
    """
    Verify all cross-references in the model are valid.

    Checks:
    - Every material_id referenced by a polygon exists in materials (ERROR)
    - Orphaned materials: defined but never referenced (WARNING)
    - Orphaned BCs: defined but never referenced in polygons' material_ids (WARNING)
    """
    issues: list[ValidationIssue] = []

    # Collect referenced material IDs from polygons
    referenced_materials: set[str] = set()
    for poly in polygons:
        if poly.material_id:
            referenced_materials.add(poly.material_id)
            if poly.material_id not in materials:
                issues.append(
                    ValidationIssue(
                        path=f"Polygon[{poly.id}]",
                        message=(
                            f"Polygon '{poly.id}' references material "
                            f"'{poly.material_id}' which is not defined."
                        ),
                        severity=Severity.ERROR,
                    ),
                )

    # Orphaned materials (defined but never used by any polygon)
    for mat_id in materials:
        if mat_id not in referenced_materials:
            issues.append(
                ValidationIssue(
                    path=f"Material[{mat_id}]",
                    message=(
                        f"Material '{mat_id}' is defined but not referenced "
                        f"by any polygon."
                    ),
                    severity=Severity.WARNING,
                ),
            )

    # Orphaned BCs are harder to detect without edge-level BC assignments.
    # THERM assigns BCs to BCPolygon edges, not to polygons directly.
    # We report if we have BCs but no polygons, which indicates a problem.
    if boundary_conditions and not polygons:
        issues.append(
            ValidationIssue(
                path="BoundaryConditions",
                message=("Boundary conditions are defined but no polygons exist."),
                severity=Severity.WARNING,
            ),
        )

    return issues
