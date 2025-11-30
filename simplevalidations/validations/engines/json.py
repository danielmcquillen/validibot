from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _
from jsonschema import Draft202012Validator
from jsonschema import FormatChecker

from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.engines.base import ValidationIssue
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.engines.registry import register_engine

if TYPE_CHECKING:
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Submission
    from simplevalidations.validations.models import Validator


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
    ) -> ValidationResult:
        if submission.file_type != SubmissionFileType.JSON:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_("This validator only accepts JSON submissions."),
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

        # Now validate!
        v = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(v.iter_errors(data), key=lambda e: list(e.path))
        issues = [
            ValidationIssue("/".join(map(str, e.path)), e.message) for e in errors
        ]
        return ValidationResult(
            passed=not issues,
            issues=issues,
            stats={"error_count": len(issues)},
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
