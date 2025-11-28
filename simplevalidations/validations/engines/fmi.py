"""
Execution stub for FMI-based validators.

The full integration will dispatch FMU execution to Modal.com after
performing safety checks. For now this engine surfaces configuration
errors clearly so authors understand the missing pieces.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _
from sv_shared.fmi import FMIRunResult
from sv_shared.fmi import FMIRunStatus

from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogRunStage
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.engines.base import ValidationIssue
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.engines.modal import ModalRunnerMixin
from simplevalidations.validations.engines.registry import register_engine

if TYPE_CHECKING:
    from simplevalidations.submissions.models import Submission
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Validator


@register_engine(ValidationType.FMI)
class FMIValidationEngine(ModalRunnerMixin, BaseValidatorEngine):
    """
    Run FMI validators by delegating execution to a sandboxed Modal function.

    This engine validates configuration then delegates execution to a Modal
    function that runs the FMU in isolation. Outputs are fed into CEL
    assertions using validator catalog slugs as signal identifiers.
    """

    modal_app_name = "fmi-runner"
    modal_function_name = "run_fmi_simulation"
    modal_return_logs_default = False

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
    ) -> ValidationResult:
        issues: list[ValidationIssue] = []
        stats: dict[str, object] = {
            "modal_app": self.modal_app_name,
            "modal_function": self.modal_function_name,
        }

        if validator.fmu_model_id:
            stats["fmu_model_id"] = validator.fmu_model_id
        else:
            issues.append(
                ValidationIssue(
                    path="",
                    message=_(
                        "This FMI validator is missing an FMU asset. "
                        "Attach an FMU before running."
                    ),
                    severity=Severity.ERROR,
                ),
            )

        if submission.file_type and not validator.supports_file_type(
            submission.file_type
        ):
            issues.append(
                ValidationIssue(
                    path="",
                    message=_(
                        "Unsupported submission file type '%(file_type)s'"
                        "for FMI validator."
                    )
                    % {"file_type": submission.file_type},
                    severity=Severity.ERROR,
                ),
            )

        run_result: FMIRunResult | None = None
        if not any(issue.severity == Severity.ERROR for issue in issues):
            try:
                payload = self._build_modal_payload(validator)
                raw_result = self._invoke_modal_runner(**payload)
                run_result = FMIRunResult.model_validate(raw_result)
                stats.update(run_result.model_dump(mode="python"))
            except Exception as exc:  # pragma: no cover - defensive
                issues.append(
                    ValidationIssue(
                        path="",
                        message=_("FMI execution failed: %(error)s")
                        % {"error": str(exc)},
                        severity=Severity.ERROR,
                    ),
                )

        if run_result and run_result.status == FMIRunStatus.ERROR:
            for error in run_result.errors:
                issues.append(
                    ValidationIssue(
                        path="",
                        message=error,
                        severity=Severity.ERROR,
                    ),
                )

        if run_result:
            if ruleset is None:
                issues.append(
                    ValidationIssue(
                        path="",
                        message=_("Ruleset is required for CEL evaluation."),
                        severity=Severity.ERROR,
                    ),
                )
            else:
                try:
                    issues.extend(
                        self.run_cel_assertions_for_stages(
                            ruleset=ruleset,
                            validator=validator,
                            input_payload=self.config.get("inputs", {}),
                            output_payload=run_result.outputs or {},
                        ),
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    issues.append(
                        ValidationIssue(
                            path="",
                            message=_("CEL evaluation failed: %(error)s")
                            % {"error": str(exc)},
                            severity=Severity.ERROR,
                        ),
                    )

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        if run_result and run_result.status != FMIRunStatus.SUCCESS:
            passed = False
        return ValidationResult(passed=passed, issues=issues, stats=stats)

    def _build_modal_payload(self, validator: Validator) -> dict[str, Any]:
        outputs = list(
            validator.catalog_entries.filter(
                run_stage=CatalogRunStage.OUTPUT,
                entry_type=CatalogEntryType.SIGNAL,
            ).values_list("slug", flat=True)
        )
        fmu_path = getattr(getattr(validator, "fmu_model", None), "file", None)
        storage_key = ""
        if fmu_path:
            storage_key = getattr(fmu_path, "path", "") or getattr(fmu_path, "name", "")
        if not storage_key:
            raise ValueError("FMU storage key is required for FMI execution.")
        fmu_checksum = ""
        if getattr(validator, "fmu_model", None):
            fmu_checksum = validator.fmu_model.checksum
        fmu_url = getattr(fmu_path, "url", None)
        use_test_volume = str(os.getenv("FMI_USE_TEST_VOLUME", "")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return {
            "fmu_storage_key": storage_key,
            "fmu_url": fmu_url or self.config.get("fmu_url"),
            "fmu_checksum": fmu_checksum or None,
            "use_test_volume": use_test_volume,
            "inputs": self.config.get("inputs", {}),
            "simulation_config": self.config.get("simulation_config", {}),
            "output_variables": outputs,
        }
