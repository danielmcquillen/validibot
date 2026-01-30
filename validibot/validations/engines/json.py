from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _
from jsonschema import Draft202012Validator
from jsonschema import FormatChecker

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


@register_engine(ValidationType.JSON_SCHEMA)
class JsonSchemaValidatorEngine(BaseValidatorEngine):
    """
    JSON Schema validator (Draft 2020-12 compatible by default if
    jsonschema lib supports).

    Expects a JSON Schema stored on the associated ruleset via ``rules_text`` or
    ``rules_file`` (retrieved through ``ruleset.rules``).
    """

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        # Store run_context on instance for CEL evaluation methods
        self.run_context = run_context
        # JSON Schema validators require JSON content. This check is a safety net -
        # the handler also validates file type compatibility before calling the engine.
        if submission.file_type != SubmissionFileType.JSON:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_(
                            "JSON Schema validators require JSON content. "
                            "Received file type: %(file_type)s"
                        ) % {"file_type": submission.file_type},
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"file_type": submission.file_type},
            )
        # Load the schema we'll be using...
        try:
            schema = self._load_schema(validator=validator, ruleset=ruleset)
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[ValidationIssue("", str(e), Severity.ERROR)],
                stats={"exception": type(e).__name__},
            )

        # Now load incoming content...
        payload = submission.get_content()

        try:
            data = json.loads(payload)
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_("Invalid JSON payload") + f": {e}",
                    ),
                ],
                stats={"exception": type(e).__name__},
            )

        # Now validate against JSON Schema!
        v = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(v.iter_errors(data), key=lambda e: list(e.path))
        issues: list[ValidationIssue] = [
            ValidationIssue("/".join(map(str, e.path)), e.message) for e in errors
        ]

        # Evaluate CEL assertions (if any) using the parsed JSON payload.
        # This follows the same pattern as BasicValidatorEngine.
        assertion_issues = self.evaluate_cel_assertions(
            ruleset=ruleset,
            validator=validator,
            payload=data,
            target_stage="input",
        )
        issues.extend(assertion_issues)

        # Count assertion failures (non-SUCCESS issues from assertions)
        # and total assertions for this stage only.
        # SUCCESS-severity issues indicate passed assertions, not failures.
        assertion_failures = sum(
            1 for issue in assertion_issues
            if issue.severity != Severity.SUCCESS
        )
        total_assertions = self._count_stage_assertions(ruleset, "input")

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=total_assertions,
                failures=assertion_failures,
            ),
            stats={"error_count": len(errors)},
        )

    def _load_schema(self, *, validator, ruleset) -> dict[str, Any]:
        raw_schema = getattr(ruleset, "rules", None)
        if not raw_schema:
            raise ValueError(
                _("Ruleset must provide schema text via rules_text or rules_file."),
            )
        if isinstance(raw_schema, dict):
            return raw_schema
        if isinstance(raw_schema, str):
            return json.loads(raw_schema)
        raise TypeError(_("Unsupported schema type; expected dict or JSON string."))
