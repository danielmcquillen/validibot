"""Canonical Portfolio Manager output groups used by runtime and authoring UI.

Single-report assertions work with facts for one property. ZIP-collection
assertions work with counts, coverage, and aggregate facts for the submitted
group. Keeping these inventories here prevents the validator, output card, and
assertion picker from disagreeing about which values apply to each structure.
"""

from __future__ import annotations

import re

from django.utils.translation import gettext_lazy as _

SINGLE_REPORT = "single_report"
ZIP_COLLECTION = "zip_collection"

SINGLE_PROPERTY_OUTPUT_KEYS = (
    "property_id",
    "parent_property_id",
    "washington_standard_id",
    "reporting_period_start",
    "reporting_period_end",
    "reporting_period_complete",
    "reporting_period_fresh",
    "gross_floor_area_ft2",
    "site_eui_kbtu_ft2_yr",
    "weather_normalized_site_eui_kbtu_ft2_yr",
    "source_eui_kbtu_ft2_yr",
    "national_median_site_eui_kbtu_ft2_yr",
    "energy_star_score",
    "heating_degree_days",
    "cooling_degree_days",
    "weather_station_id",
    "weather_station_name",
    "resolved_euit_kbtu_ft2_yr",
    "resolved_euit_source",
    "euit_margin_kbtu_ft2_yr",
    "euit_ratio",
    "euit_percent_difference",
    "meets_euit",
    "near_euit",
    "benchmark_ready",
    "form_c_ready",
)

GROUPED_PROPERTY_OUTPUT_KEYS = (
    "submission_structure",
    "file_count",
    "valid_file_count",
    "invalid_file_count",
    "property_count",
    "reporting_cycle_count",
    "reporting_cycles_match",
    "complete_reporting_period_property_count",
    "fresh_reporting_period_property_count",
    "expected_building_count",
    "matched_expected_building_count",
    "missing_expected_building_count",
    "unexpected_submitted_building_count",
    "duplicate_submitted_property_count",
    "parent_child_overlap_count",
    "target_covered_property_count",
    "target_uncovered_property_count",
    "target_comparable_property_count",
    "target_met_property_count",
    "target_above_property_count",
    "target_near_property_count",
    "benchmark_ready_property_count",
    "form_c_ready_property_count",
    "aggregate_metrics_available",
    "total_gross_floor_area_ft2",
    "weighted_weather_normalized_site_eui_kbtu_ft2_yr",
    "energy_star_score_property_count",
    "weighted_energy_star_score",
    "estimated_excess_energy_kbtu",
    "target_coverage_percent",
    "target_compliance_percent",
    "floor_area_target_compliance_percent",
)

ALL_PROPERTY_OUTPUT_KEYS = frozenset(
    (*SINGLE_PROPERTY_OUTPUT_KEYS, *GROUPED_PROPERTY_OUTPUT_KEYS)
)

_OUTPUT_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:o|output)\.([A-Za-z_][A-Za-z0-9_]*)"
)
_BRACKET_OUTPUT_REFERENCE_RE = re.compile(
    r"""(?<![A-Za-z0-9_.])(?:o|output)\s*\[\s*(["'])"""
    r"""([A-Za-z_][A-Za-z0-9_]*)\1\s*\]"""
)


def output_keys_for_structure(submission_structure: str) -> frozenset[str]:
    """Return the scalar outputs authors may use for one submission structure."""
    if submission_structure == ZIP_COLLECTION:
        return frozenset(GROUPED_PROPERTY_OUTPUT_KEYS)
    return frozenset(SINGLE_PROPERTY_OUTPUT_KEYS)


def output_group_label(submission_structure: str) -> str:
    """Return the concise author-facing heading for the selected output group."""
    if submission_structure == ZIP_COLLECTION:
        return _("Grouped property outputs")
    return _("Single property outputs")


def referenced_output_keys(expression: str) -> set[str]:
    """Return ``o``/``output`` references outside ordinary CEL strings."""
    expression = expression or ""
    outside_strings = _outside_string_positions(expression)
    dot_keys = {
        match.group(1)
        for match in _OUTPUT_REFERENCE_RE.finditer(expression)
        if outside_strings[match.start()]
    }
    bracket_keys = {
        match.group(2)
        for match in _BRACKET_OUTPUT_REFERENCE_RE.finditer(expression)
        if outside_strings[match.start()]
    }
    return dot_keys | bracket_keys


def _outside_string_positions(expression: str) -> list[bool]:
    """Mark characters that are not inside a quoted CEL string."""
    positions: list[bool] = []
    quote: str | None = None
    escaped = False

    for char in expression:
        if quote is None:
            positions.append(True)
            if char in {"'", '"'}:
                quote = char
                escaped = False
            continue
        positions.append(False)
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            quote = None

    return positions
