"""Material property validation for THERM models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from validibot.validations.constants import Severity
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.therm.constants import CONDUCTIVITY_MAX
from validibot.validations.engines.therm.constants import CONDUCTIVITY_MIN
from validibot.validations.engines.therm.constants import EMISSIVITY_MAX
from validibot.validations.engines.therm.constants import EMISSIVITY_MIN

if TYPE_CHECKING:
    from validibot.validations.engines.therm.models import ThermMaterial


def check_material_properties(
    materials: dict[str, ThermMaterial],
) -> list[ValidationIssue]:
    """
    Validate material property ranges.

    Checks:
    - Thermal conductivity must be positive (ERROR if <= 0)
    - Thermal conductivity within [0.01, 500] W/m-K (WARNING if outside)
    - Emissivity values within [0, 1] (WARNING if outside)
    """
    issues: list[ValidationIssue] = []

    for mat in materials.values():
        if mat.conductivity is not None:
            if mat.conductivity <= 0:
                issues.append(
                    ValidationIssue(
                        path=f"Material[{mat.name}]",
                        message=(
                            f"Material '{mat.name}' has conductivity "
                            f"{mat.conductivity} W/m-K. "
                            f"Conductivity must be positive (zero causes "
                            f"FEM solver failure)."
                        ),
                        severity=Severity.ERROR,
                    ),
                )
            elif (
                mat.conductivity < CONDUCTIVITY_MIN
                or mat.conductivity > CONDUCTIVITY_MAX
            ):
                issues.append(
                    ValidationIssue(
                        path=f"Material[{mat.name}]",
                        message=(
                            f"Material '{mat.name}' has conductivity "
                            f"{mat.conductivity} W/m-K, outside the typical "
                            f"range [{CONDUCTIVITY_MIN}, {CONDUCTIVITY_MAX}]."
                        ),
                        severity=Severity.WARNING,
                    ),
                )

        # Emissivity checks (inside and outside surfaces)
        for attr_name, label in [
            ("emissivity_inside", "inside emissivity"),
            ("emissivity_outside", "outside emissivity"),
        ]:
            value = getattr(mat, attr_name)
            if value is not None and (value < EMISSIVITY_MIN or value > EMISSIVITY_MAX):
                issues.append(
                    ValidationIssue(
                        path=f"Material[{mat.name}]",
                        message=(
                            f"Material '{mat.name}' has {label} "
                            f"{value}, outside range "
                            f"[{EMISSIVITY_MIN}, {EMISSIVITY_MAX}]."
                        ),
                        severity=Severity.WARNING,
                    ),
                )

    return issues
