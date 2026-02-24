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
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.base.registry import register_validator

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Submission
    from validibot.validations.models import Validator


@register_validator(ValidationType.JSON_SCHEMA)
class JsonSchemaValidator(BaseValidator):
    """
    JSON Schema validator (Draft 2020-12 compatible).

    This is a **schema-only** validator. It validates JSON documents against a
    JSON Schema and reports structural violations. It does NOT support ruleset
    assertions (BASIC or CEL) - use the Basic validator type for assertion-based
    validation of JSON data.

    Expects a JSON Schema stored on the associated ruleset via ``rules_text`` or
    ``rules_file`` (retrieved through ``ruleset.rules``).
    """

    # PUBLIC METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate a JSON document against the configured JSON Schema.

        Parses the submission content as JSON and validates it against the
        Draft 2020-12 JSON Schema stored in the ruleset. Returns ERROR issues
        for any schema violations.
        """
        # JSON Schema validators require JSON content. This check is a safety
        # net - the handler also validates file type before calling validate().
        if submission.file_type != SubmissionFileType.JSON:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_(
                            "JSON Schema validators require JSON content. "
                            "Received file type: %(file_type)s"
                        )
                        % {"file_type": submission.file_type},
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

        # Validate against JSON Schema
        v = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(v.iter_errors(data), key=lambda e: list(e.path))
        issues: list[ValidationIssue] = [
            ValidationIssue("/".join(map(str, e.path)), e.message) for e in errors
        ]

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            stats={"error_count": len(errors)},
        )

    # PRIVATE METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

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
