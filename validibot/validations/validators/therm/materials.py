"""
Material property validation for THERM models.

TODO: Implement domain checks based on original research — testing
THERM files with out-of-range or missing material values and
documenting how THERM handles them.

Planned check categories:
- Conductivity sanity (positive, within physically reasonable range)
- Emissivity bounds
- Required fields present
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.validations.validators.base.base import ValidationIssue
    from validibot.validations.validators.therm.models import ThermMaterial


def run_material_checks(
    materials: dict[str, ThermMaterial],
) -> list[ValidationIssue]:
    """
    Run all material property checks.

    Returns a list of ValidationIssue objects for any problems found.

    TODO: Implement specific checks. Property ranges and severity
    levels should be determined by testing actual THERM behavior.
    """
    issues: list[ValidationIssue] = []
    # TODO: implement material checks
    return issues
