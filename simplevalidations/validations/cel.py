from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CelHelper:
    """Describes a single CEL helper function exposed to assertions."""

    name: str
    signature: str
    return_type: str
    description: str


DEFAULT_HELPERS: dict[str, CelHelper] = {
    "has": CelHelper(
        name="has",
        signature="has(value)",
        return_type="bool",
        description="Returns true when the value is not null/empty.",
    ),
    "is_int": CelHelper(
        name="is_int",
        signature="is_int(value)",
        return_type="bool",
        description="Returns true when the numeric value is an integer.",
    ),
    "percentile": CelHelper(
        name="percentile",
        signature="percentile(values, q)",
        return_type="number",
        description="Calculates the q-quantile for the numeric list.",
    ),
    "mean": CelHelper(
        name="mean",
        signature="mean(values)",
        return_type="number",
        description="Average of a list of numbers (ignores nulls).",
    ),
    "sum": CelHelper(
        name="sum",
        signature="sum(values)",
        return_type="number",
        description="Sum of a list of numbers.",
    ),
    "max": CelHelper(
        name="max",
        signature="max(values)",
        return_type="number",
        description="Maximum value in a list of numbers.",
    ),
    "min": CelHelper(
        name="min",
        signature="min(values)",
        return_type="number",
        description="Minimum value in a list of numbers.",
    ),
    "abs": CelHelper(
        name="abs",
        signature="abs(value)",
        return_type="number",
        description="Absolute value of a number.",
    ),
    "round": CelHelper(
        name="round",
        signature="round(value, digits)",
        return_type="number",
        description="Round the numeric value to a number of decimal places.",
    ),
    "duration": CelHelper(
        name="duration",
        signature="duration(series, predicate)",
        return_type="number",
        description="Calculates the duration (in sample count) where predicate(series[i]) is true.",
    ),
}
