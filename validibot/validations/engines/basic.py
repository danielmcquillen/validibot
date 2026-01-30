"""
Basic validation engine.

Evaluates BASIC assertions against JSON submissions. Assertions are defined
with paths (e.g., "payload.items[0].price") and operators (eq, ne, gt, etc.).
All assertion evaluation is delegated to the unified assertion system via
the BasicAssertionEvaluator.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.engines.base import AssertionStats
from validibot.validations.engines.base import BaseValidatorEngine
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.base import ValidationResult
from validibot.validations.engines.registry import register_engine

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Submission
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)


@register_engine(ValidationType.BASIC)
class BasicValidatorEngine(BaseValidatorEngine):
    """
    Validates a submission by evaluating the BASIC assertions stored on a ruleset.

    The submission content must be JSON. Targets are resolved via dot / [index]
    paths (for example, ``payload.items[0].price``). Each assertion carries its
    operator payload (rhs/options) which we evaluate inline here.
    """

    # PUBLIC METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate a submission by evaluating all assertions in order.

        Uses the unified assertion evaluation system which dispatches to
        type-specific evaluators (BASIC, CEL, etc.) registered in the registry.
        """
        # Store run_context on instance for assertion evaluation
        self.run_context = run_context

        # BasicValidatorEngine requires JSON content since it parses and evaluates
        # assertions against JSON paths. This check is a safety net - the handler
        # also validates file type compatibility before calling the engine.
        if submission.file_type != SubmissionFileType.JSON:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_(
                            "Basic validators require JSON content. "
                            "Received file type: %(file_type)s"
                        ) % {"file_type": submission.file_type},
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"file_type": submission.file_type},
            )

        raw_content = submission.get_content()

        try:
            payload = json.loads(raw_content)
        except Exception as exc:  # pragma: no cover - JSON error path
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_(
                            "Invalid JSON submission: %(error)s",
                        ) % {"error": exc},
                    ),
                ],
                stats={"exception": type(exc).__name__},
            )

        # Evaluate all assertions using the unified system.
        # Basic validators have no external processor, so we evaluate both
        # input-stage and output-stage assertions together.
        issues: list[ValidationIssue] = []
        total_assertions = 0
        total_failures = 0

        for stage in ("input", "output"):
            result = self.evaluate_assertions_for_stage(
                validator=validator,
                ruleset=ruleset,
                payload=payload,
                stage=stage,
            )
            issues.extend(result.issues)
            total_assertions += result.total
            total_failures += result.failures

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=total_assertions,
                failures=total_failures,
            ),
        )

