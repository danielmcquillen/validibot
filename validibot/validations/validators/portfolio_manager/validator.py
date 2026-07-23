"""Django-side orchestration for the isolated Portfolio Manager backend."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError

from validibot.validations.constants import PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES
from validibot.validations.validators.base.advanced import AdvancedValidator

_COLLECTION_OUTPUT_KEYS = (
    "profile",
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
_SINGLE_OUTPUT_KEYS = (
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


class PortfolioManagerValidator(AdvancedValidator):
    """Launch the first-party Portfolio Manager backend and expose its facts."""

    @property
    def validator_display_name(self) -> str:
        """Return the author-facing backend name used in shared errors."""
        return "Portfolio Manager"

    def preprocess_submission(self, *, step, submission) -> dict[str, object]:
        """Reject a mode/extension mismatch before spending container compute."""
        structure = (step.config or {}).get("submission_structure", "single_report")
        suffix = Path(submission.original_filename or "").suffix.casefold()
        expected = (
            {".zip"} if structure == "zip_collection" else {".xls", ".xlsx", ".xml"}
        )
        if suffix not in expected:
            if structure == "zip_collection":
                message = "ZIP collection mode requires one .zip submission."
            else:
                message = (
                    "Single-report mode requires a .xls, .xlsx, or .xml submission."
                )
            raise ValidationError(message)
        if submission.size_bytes > PORTFOLIO_MANAGER_MAX_SUBMISSION_BYTES:
            raise ValidationError(
                "Portfolio Manager submissions must be 500 MB or smaller."
            )
        return {"submission_structure": structure}

    def _resolve_input_stage_payload(self, submission) -> None:
        """Avoid replacement-text decoding of binary spreadsheet/archive bytes."""

    def extract_output_values(self, output_envelope: Any) -> dict[str, Any] | None:
        """Project typed backend facts into the catalog-controlled ``o.*`` surface."""
        outputs = getattr(output_envelope, "outputs", None)
        if outputs is None:
            return None
        values = {
            key: _json_number(getattr(outputs, key, None))
            for key in _COLLECTION_OUTPUT_KEYS
        }
        record = (
            outputs.property_results[0] if len(outputs.property_results) == 1 else None
        )
        for key in _SINGLE_OUTPUT_KEYS:
            values[key] = _json_number(getattr(record, key, None)) if record else None
        return values


def _json_number(value: Any) -> Any:
    """Convert Decimal/date values to CEL- and JSON-safe scalar representations."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value
