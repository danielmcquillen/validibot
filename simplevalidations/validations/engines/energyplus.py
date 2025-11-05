"""
EnergyPlus validation engine powered by the Modal runner.

This engine forwards incoming EnergyPlus submissions (epJSON or IDF) to the
Modal function defined in ``sv_modal.projects.sv_energyplus`` and translates
the response into SimpleValidations issues.

The response is a typed ``EnergyPlusSimulationResult`` model defined in
``sv_shared.energyplus.models``. We can use that model for raw data to
compare against the user's configured checks.

Additional static checks can be layered on once the runner exposes the
necessary signals.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _
from sv_shared.energyplus.models import EnergyPlusSimulationMetrics
from sv_shared.energyplus.models import EnergyPlusSimulationResult

from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.engines.base import ValidationIssue
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.engines.modal import ModalRunnerMixin
from simplevalidations.validations.engines.registry import register_engine

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from simplevalidations.submissions.models import Submission
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Validator


@register_engine(ValidationType.ENERGYPLUS)
class EnergyPlusValidationEngine(ModalRunnerMixin, BaseValidatorEngine):
    """
    Run submitted epJSON through the Modal EnergyPlus runner and translate the
    response into SimpleValidations issues.

    Requirements:
    * The workflow step must enable ``run_simulation`` (static IDF checks are not
      implemented yet).
    * Provide a weather file name via the ruleset metadata
      (``ruleset.metadata['weather_file']``) or the
      ``ENERGYPLUS_DEFAULT_WEATHER`` environment variable.

    Optional cleanup:
    * Set ``cleanup_after_run`` in the engine config to request the Modal cleanup
      function after results are processed. ``cleanup_missing_ok`` can be used to
      control whether missing output directories raise errors.
    """

    modal_app_name = "energyplus-runner"
    modal_function_name = "run_energyplus_simulation"
    modal_return_logs_env_var = "ENERGYPLUS_MODAL_RETURN_LOGS"
    modal_cleanup_function_name = "cleanup_simulation_outputs"

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
    ) -> ValidationResult:
        provider = self.resolve_provider(validator)
        if provider:
            provider.ensure_catalog_entries()

        config = self.config or {}
        run_simulation = bool(config.get("run_simulation", True))

        # The 'stats' dict is the per-run telemetry we attach to the
        # ValidationResult, giving downstream callers insight into how
        # the simulation was invoked (modal function,
        # weather file, run duration, outputs, cleanup status, etc.)
        stats: dict[str, Any] = {
            "modal_app": self.modal_app_name,
            "modal_function": self.modal_function_name,
            "run_simulation": run_simulation,
        }
        issues: list[ValidationIssue] = []

        # We will do the syntax check within the modal function.

        if not run_simulation:
            issues.append(
                ValidationIssue(
                    path="",
                    message=_(
                        "EnergyPlus simulation execution is disabled for this step. "
                        "Enable 'Run EnergyPlus simulation' until static IDF checks "
                        "are available.",
                    ),
                    severity=Severity.ERROR,
                ),
            )
            return ValidationResult(passed=False, issues=issues, stats=stats)

        try:
            energyplus_payload = submission.get_content()
        except Exception as exc:  # pragma: no cover - defensive read failure
            logger.exception("Unable to load submission content for EnergyPlus.")
            issues.append(
                ValidationIssue(
                    path="",
                    message=_("Unable to read submission content: %(error)s")
                    % {"error": exc},
                    severity=Severity.ERROR,
                ),
            )
            return ValidationResult(passed=False, issues=issues, stats=stats)

        stripped_payload = energyplus_payload.strip()
        if not stripped_payload:
            issues.append(
                ValidationIssue(
                    path="",
                    message=_("Submission has no EnergyPlus content."),
                    severity=Severity.ERROR,
                ),
            )
            return ValidationResult(passed=False, issues=issues, stats=stats)

        weather_file = self._resolve_weather_file(ruleset)
        stats["weather_file"] = weather_file

        idf_checks = config.get("idf_checks") or []
        simulation_checks = config.get("simulation_checks") or []
        stats["requested_idf_checks"] = idf_checks
        stats["requested_simulation_checks"] = simulation_checks

        if idf_checks:
            issues.append(
                ValidationIssue(
                    path="",
                    message=_(
                        "IDF checks (%(checks)s) are not implemented yet; only the "
                        "EnergyPlus simulation runs at this stage.",
                    )
                    % {"checks": ", ".join(sorted(idf_checks))},
                    severity=Severity.WARNING,
                ),
            )

        try:
            energyplus_payload_arg: Any = stripped_payload
            if stripped_payload.startswith("{"):
                try:
                    energyplus_payload_arg = json.loads(stripped_payload)
                except json.JSONDecodeError:
                    energyplus_payload_arg = stripped_payload

            raw_result = self._invoke_modal_runner(
                energyplus_payload=energyplus_payload_arg,
                weather_file=weather_file,
                simulation_id=str(submission.id),
            )
        except Exception as exc:
            logger.exception("EnergyPlus Modal invocation failed.")
            issues.append(
                ValidationIssue(
                    path="",
                    message=_("Failed to execute EnergyPlus via Modal: %(error)s")
                    % {"error": exc},
                    severity=Severity.ERROR,
                ),
            )
            stats["modal_error"] = str(exc)
            return ValidationResult(passed=False, issues=issues, stats=stats)

        typed_result: EnergyPlusSimulationResult = self._parse_modal_result(
            raw_result,
            issues,
            stats,
        )
        if typed_result is None:
            return ValidationResult(passed=False, issues=issues, stats=stats)

        stats["simulation_id"] = typed_result.simulation_id
        stats["execution_seconds"] = typed_result.execution_seconds
        stats["invocation_mode"] = typed_result.invocation_mode
        stats["energyplus_returncode"] = typed_result.energyplus_returncode
        stats["messages"] = list(typed_result.messages)
        # `mode="json"` ensures Path fields inside the Pydantic model 
        # are serialized as strings.
        stats["outputs"] = typed_result.outputs.model_dump(
            mode="json",
            exclude_none=True,
        )
        stats["metrics"] = typed_result.metrics.model_dump(
            mode="json",
            exclude_none=True,
        )
        if typed_result.logs is not None:
            stats["logs"] = typed_result.logs.model_dump(
                mode="json",
                exclude_none=True,
            )
        energyplus_input_path = getattr(
            typed_result,
            "energyplus_input_file_path",
            None,
        )
        if energyplus_input_path is not None:
            stats["energyplus_input_file_path"] = str(energyplus_input_path)
        payload_format = getattr(
            typed_result,
            "energyplus_payload_format",
            None,
        )
        if payload_format is not None:
            stats["energyplus_payload_format"] = payload_format

        cleanup_after_run = bool(config.get("cleanup_after_run"))
        if cleanup_after_run:
            stats["cleanup_requested"] = True
            cleanup_missing_ok = bool(config.get("cleanup_missing_ok", True))
            try:
                cleanup_result = self._invoke_modal_cleanup(
                    simulation_id=typed_result.simulation_id,
                    missing_ok=cleanup_missing_ok,
                )
            except Exception as exc:  # pragma: no cover - defensive cleanup failure
                logger.exception(
                    "EnergyPlus cleanup failed for simulation %s",
                    typed_result.simulation_id,
                )
                issues.append(
                    ValidationIssue(
                        path="",
                        message=_("EnergyPlus cleanup failed: %(error)s")
                        % {"error": exc},
                        severity=Severity.WARNING,
                    ),
                )
                stats["cleanup_error"] = str(exc)
            else:
                stats["cleanup_result"] = cleanup_result

        if typed_result.status != "success":
            issues.extend(
                [
                    ValidationIssue(
                        path="",
                        message=error_msg,
                        severity=Severity.ERROR,
                    )
                    for error_msg in typed_result.errors or []
                ],
            )
            return ValidationResult(passed=False, issues=issues, stats=stats)

        issues.extend(
            self._run_simulation_checks(
                simulation_checks=simulation_checks,
                eui_band=config.get("eui_band") or {},
                metrics=typed_result.metrics,
            ),
        )

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(passed=passed, issues=issues, stats=stats)

    def _resolve_weather_file(self, ruleset: Ruleset | None) -> str:
        if ruleset and isinstance(getattr(ruleset, "metadata", None), dict):
            candidate = (ruleset.metadata or {}).get("weather_file")
            if candidate:
                return str(candidate)
        env_default = os.getenv("ENERGYPLUS_DEFAULT_WEATHER")
        if env_default:
            return env_default
        raise RuntimeError(
            _(
                "EnergyPlus ruleset must define metadata['weather_file'] or set "
                "ENERGYPLUS_DEFAULT_WEATHER.",
            ),
        )

    def _parse_modal_result(
        self,
        raw_result: Any,
        issues: list[ValidationIssue],
        stats: dict[str, Any],
    ) -> EnergyPlusSimulationResult | None:
        if EnergyPlusSimulationResult is None:
            issues.append(
                ValidationIssue(
                    path="",
                    message=_(
                        "sv_modal SimulationResult model is unavailable. "
                        "Ensure the sv_modal repository is accessible.",
                    ),
                    severity=Severity.ERROR,
                ),
            )
            stats["modal_result_raw"] = raw_result
            return None
        try:
            if isinstance(raw_result, dict):
                if (
                    "epjson_path" in raw_result
                    and "energyplus_input_file_path" not in raw_result
                ):
                    raw_result["energyplus_input_file_path"] = raw_result[
                        "epjson_path"
                    ]
                raw_result.pop("epjson_path", None)
            return EnergyPlusSimulationResult.model_validate(raw_result)
        except Exception as exc:
            logger.exception("Unable to parse EnergyPlus result payload.")
            issues.append(
                ValidationIssue(
                    path="",
                    message=_("Unable to parse EnergyPlus result payload: %(error)s")
                    % {"error": exc},
                    severity=Severity.ERROR,
                ),
            )
            stats["modal_result_raw"] = raw_result
            return None

    def _run_simulation_checks(
        self,
        *,
        simulation_checks: list[str],
        eui_band: dict[str, Any],
        metrics: EnergyPlusSimulationMetrics,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if "eui-range" in simulation_checks:
            eui_value = getattr(metrics, "energy_use_intensity_kwh_m2", None)
            min_val = eui_band.get("min")
            max_val = eui_band.get("max")
            if eui_value is None:
                issues.append(
                    ValidationIssue(
                        path="metrics.energy_use_intensity_kwh_m2",
                        message=_(
                            "EnergyPlus run did not expose an Energy Use Intensity "
                            "value, so the configured range check could not run.",
                        ),
                        severity=Severity.WARNING,
                    ),
                )
            else:
                if min_val is not None and eui_value < float(min_val):
                    issues.append(
                        ValidationIssue(
                            path="metrics.energy_use_intensity_kwh_m2",
                            message=_(
                                "Energy Use Intensity %(value)s kWh/m² is below the "
                                "minimum %(threshold)s kWh/m².",
                            )
                            % {"value": eui_value, "threshold": min_val},
                            severity=Severity.ERROR,
                        ),
                    )
                if max_val is not None and eui_value > float(max_val):
                    issues.append(
                        ValidationIssue(
                            path="metrics.energy_use_intensity_kwh_m2",
                            message=_(
                                "Energy Use Intensity %(value)s kWh/m² exceeds the "
                                "maximum %(threshold)s kWh/m².",
                            )
                            % {"value": eui_value, "threshold": max_val},
                            severity=Severity.ERROR,
                        ),
                    )
        unsupported = sorted(set(simulation_checks) - {"eui-range"})
        if unsupported:
            issues.append(
                ValidationIssue(
                    path="",
                    message=_("Simulation checks %(checks)s are not implemented yet.")
                    % {"checks": ", ".join(unsupported)},
                    severity=Severity.WARNING,
                ),
            )
        return issues


def configure_modal_runner(
    mock_callable: Callable[..., Any] | None,
    *,
    cleanup_callable: Callable[..., Any] | None = None,
) -> None:
    """
    Backwards-compatible helper to configure the Modal runner for tests.
    """

    EnergyPlusValidationEngine.configure_modal_runner(
        mock_callable,
        cleanup_callable=cleanup_callable,
    )
