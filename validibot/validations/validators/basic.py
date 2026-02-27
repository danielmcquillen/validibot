"""
Basic validator

Evaluates BASIC assertions against JSON and XML submissions. Assertions are
defined with paths (e.g., "payload.items[0].price") and operators (eq, ne,
gt, etc.). All assertion evaluation is delegated to the unified assertion
system via the BasicAssertionEvaluator.

For XML submissions, the XML is converted to a nested dict via
``xml_to_dict()`` so that CEL expressions and path-based assertions work
identically to JSON — the XML never hits the evaluator directly.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.base.registry import register_validator
from validibot.validations.xml_utils import XmlParseError
from validibot.validations.xml_utils import xml_to_dict

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Submission
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)


@register_validator(ValidationType.BASIC)
class BasicValidator(BaseValidator):
    """
    Validates a submission by evaluating the BASIC assertions stored on a ruleset.

    Accepts JSON and XML submissions. Targets are resolved via dot / [index]
    paths (for example, ``payload.items[0].price``). For XML, the document is
    first converted to a nested dict so paths and CEL expressions work
    identically to JSON.
    """

    _SUPPORTED_FILE_TYPES = frozenset({SubmissionFileType.JSON, SubmissionFileType.XML})

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

        # BasicValidator accepts JSON and XML. This check is a safety net —
        # the handler also validates file type compatibility before calling.
        if submission.file_type not in self._SUPPORTED_FILE_TYPES:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_(
                            "Basic validators require JSON or XML content. "
                            "Received file type: %(file_type)s"
                        )
                        % {"file_type": submission.file_type},
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"file_type": submission.file_type},
            )

        raw_content = submission.get_content()

        # Parse submission content into a dict. The XML-to-dict conversion
        # happens once here; the resulting payload is reused for all
        # assertions (both BASIC and CEL) without re-parsing.
        payload: dict | list | None = None
        if submission.file_type == SubmissionFileType.JSON:
            try:
                payload = json.loads(raw_content)
            except Exception as exc:
                return ValidationResult(
                    passed=False,
                    issues=[
                        ValidationIssue(
                            path="",
                            message=_(
                                "Invalid JSON submission: %(error)s",
                            )
                            % {"error": exc},
                        ),
                    ],
                    stats={"exception": type(exc).__name__},
                )
        elif submission.file_type == SubmissionFileType.XML:
            try:
                payload = xml_to_dict(raw_content)
            except XmlParseError as exc:
                return ValidationResult(
                    passed=False,
                    issues=[
                        ValidationIssue(
                            path="",
                            message=_(
                                "Invalid XML submission: %(error)s",
                            )
                            % {"error": exc},
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
