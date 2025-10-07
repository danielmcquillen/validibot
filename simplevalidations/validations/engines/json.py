from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from django.utils.translation import gettext as _
from jsonschema import Draft202012Validator, FormatChecker

from simplevalidations.validations.constants import Severity, ValidationType
from simplevalidations.validations.engines.base import (
    BaseValidatorEngine,
    ValidationIssue,
    ValidationResult,
)
from simplevalidations.validations.engines.registry import register_engine

if TYPE_CHECKING:
    from simplevalidations.validations.models import Ruleset, Submission, Validator


@register_engine(ValidationType.JSON_SCHEMA)
class JsonSchemaValidatorEngine(BaseValidatorEngine):
    """
    JSON Schema validator (Draft 2020-12 compatible by default if jsonschema lib supports).
    Expects a JSON Schema under (priority):
       1) ruleset.config['schema']
       2) validator.config['schema']
       3) self.config['schema'] (fallback if you injected one)

     Example config on your Validator model:
       {
         "schema": {
           "$schema": "https://json-schema.org/draft/2020-12/schema",
           "type": "object",
           "required": ["version"],
           "properties": { "version": { "type": "string" } },
           "additionalProperties": false
         }
       }
    """

    def _load_schema(self, *, validator, ruleset) -> dict[str, Any]:
        raw_schema = None

        # Right now we expect the schema to be stored in
        # ruleset.metadata under the 'schema' key
        try:
            raw_schema = ruleset.metadata.get("schema", None)
        except Exception:
            raise ValueError(_("Missing 'schema' in ruleset/validator config."))
        if isinstance(raw_schema, dict):
            return raw_schema
        if isinstance(raw_schema, str):
            return json.loads(raw_schema)
        raise TypeError(_("Unsupported schema type; expected dict or JSON string."))

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
    ) -> ValidationResult:
        # Load the schema we'll be using...
        try:
            schema = self._load_schema(validator=validator, ruleset=ruleset)
        except Exception as e:
            return ValidationResult(
                False,
                [ValidationIssue("", str(e), Severity.ERROR)],
                {"exception": type(e).__name__},
            )

        # Now load incoming content...
        payload = submission.get_content()

        try:
            data = json.loads(payload)
        except Exception as e:  # noqa: BLE001
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
