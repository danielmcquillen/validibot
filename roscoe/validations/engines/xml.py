from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _
from lxml import etree

from roscoe.validations.constants import Severity
from roscoe.validations.constants import ValidationType
from roscoe.validations.engines.base import BaseValidatorEngine
from roscoe.validations.engines.base import ValidationIssue
from roscoe.validations.engines.base import ValidationResult
from roscoe.validations.engines.registry import register_engine

if TYPE_CHECKING:
    from roscoe.validations.models import Ruleset
    from roscoe.validations.models import Submission
    from roscoe.validations.models import Validator


@register_engine(ValidationType.XML_SCHEMA)
class XmlSchemaValidator(BaseValidatorEngine):
    """
    XML Schema (XSD) validator.

    Expects a 'schema' entry in config:
      - str: the XSD schema as a string

    Example config on your Validator model:
      {
        "schema": "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>...</xs:schema>"
      }
    """

    def _load_schema(self) -> Any:
        """
        Parse the XSD schema string and return an lxml.etree.XMLSchema object.
        """
        raw = self.config.get("schema")
        if raw is None:
            raise ValueError(_("Missing 'schema' in validator config."))
        if isinstance(raw, str):
            try:
                schema_root = etree.XML(raw.encode("utf-8"))
                return etree.XMLSchema(schema_root)
            except Exception as e:
                raise ValueError(f"Invalid XSD schema: {e}") from e
        raise TypeError(_("Unsupported schema type; expected string."))

    def validate_text(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
    ) -> ValidationResult:
        """
        Validate the provided XML text against the configured XSD schema.
        Returns a ValidationResult with ERROR issues for any schema violations.
        """
        # Parse XML payload

        content = submission.get_content()

        try:
            parser = etree.XMLParser(recover=False)
            doc = etree.fromstring(content.encode("utf-8"), parser=parser)
        except Exception as e:  # noqa: BLE001
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=f"Invalid XML payload: {e}",
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"exception": type(e).__name__},
            )

        # Load/compile schema
        try:
            schema = self._load_schema()
        except Exception as e:  # noqa: BLE001
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=str(e),
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"exception": type(e).__name__},
            )

        # Validate and collect issues
        is_valid = schema.validate(doc)
        if is_valid:
            return ValidationResult(passed=True, issues=[], stats={"error_count": 0})

        # Extract details from the schema's error log
        issues: list[ValidationIssue] = []
        for err in schema.error_log:
            # lxml error logs include message, line, column; path may be
            # available in some cases
            path = (
                getattr(err, "path", "")
                or f"$ (line {getattr(err, 'line', '?')}, column {getattr(err, 'column', '?')})"  # noqa: E501
            )
            issues.append(
                ValidationIssue(
                    path=path,
                    message=str(err.message),
                    severity=Severity.ERROR,
                ),
            )

        return ValidationResult(
            passed=False,
            issues=issues,
            stats={
                "error_count": len(issues),
                "first_error": issues[0].message if issues else None,
            },
        )
