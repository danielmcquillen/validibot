from __future__ import annotations

import io
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.base.registry import register_validator

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator


@register_validator(ValidationType.XML_SCHEMA)
class XmlSchemaValidator(BaseValidator):
    """
    XML validator that supports XSD (default), Relax NG, and DTD.

    This is a **schema-only** validator. It validates XML documents against an
    XML schema (XSD, Relax NG, or DTD) and reports structural violations. It does
    NOT support ruleset assertions (BASIC or CEL) - assertions are not applicable
    to XML schema validation.

    Ruleset requirements:
      * ``ruleset.metadata['schema_type']`` must be one of ``XMLSchemaType``.
      * ``ruleset.rules_text`` or ``ruleset.rules_file`` should provide the schema text.

    For legacy rulesets that did not embed the schema, we fall back to
    ``validator.config['schema']``. New rulesets should keep the schema in
    metadata so it travels with the reusable asset.
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
        Validate the provided XML against the configured schema (XSD or Relax NG).
        Returns a ValidationResult with ERROR issues for any schema violations.
        """
        if submission.file_type != SubmissionFileType.XML:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("This validator only accepts XML submissions."),
                        Severity.ERROR,
                    ),
                ],
                stats={"file_type": submission.file_type},
            )
        # lxml optional (import lazily)
        try:
            from lxml import etree
        except Exception as e:  # pragma: no cover
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("lxml not installed or unusable: ") + str(e),
                        Severity.ERROR,
                    ),
                ],
                stats={"exception": type(e).__name__},
            )

        schema_type = self._resolve_schema_type(ruleset)
        raw = self._get_schema_raw(validator=validator, ruleset=ruleset)
        if not raw:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("Missing 'schema' in ruleset/validator config."),
                        Severity.ERROR,
                    ),
                ],
                stats={"schema_type": schema_type},
            )

        # Parse input
        # Prefer Submission.get_content() if available
        try:
            content = submission.get_content() if submission else None
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("Could not read submission content: ") + str(e),
                        Severity.ERROR,
                    ),
                ],
                stats={"schema_type": schema_type, "exception": type(e).__name__},
            )
        if not content:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("Empty submission content."),
                        Severity.ERROR,
                    ),
                ],
                stats={"schema_type": schema_type},
            )

        # Parse XML payload
        try:
            parser = etree.XMLParser(
                recover=False,
                resolve_entities=False,
                no_network=True,
            )
            doc = etree.fromstring((content or "").encode("utf-8"), parser=parser)
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        "",
                        _("Invalid XML payload: ") + str(e),
                        Severity.ERROR,
                    ),
                ],
                stats={"schema_type": schema_type, "exception": type(e).__name__},
            )

        # Compile schema and validate
        try:
            schema = self._load_schema(schema_type=schema_type, raw=raw)
        except Exception as e:
            return ValidationResult(
                passed=False,
                issues=[ValidationIssue("", str(e), Severity.ERROR)],
                stats={"schema_type": schema_type, "exception": type(e).__name__},
            )

        ok = schema.validate(doc)
        issues: list[ValidationIssue] = []

        if not ok:
            for err in getattr(schema, "error_log", []) or []:
                if self._is_cascade_error(err):
                    continue
                path = self._extract_error_path(err)
                issues.append(ValidationIssue(path, str(err.message), Severity.ERROR))

        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            stats={
                "error_count": len(issues),
                "schema_type": schema_type,
            },
        )

    # PRIVATE METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def _resolve_schema_type(self, ruleset) -> str:
        schema_type = None
        if ruleset is not None:
            metadata = getattr(ruleset, "metadata", None) or {}
            if isinstance(metadata, dict):
                schema_type = (metadata.get("schema_type") or "").strip().upper()
        # Expect the upper-case string of the enum's value (e.g., "XSD" or "RELAXNG")
        if schema_type not in {
            XMLSchemaType.XSD,
            XMLSchemaType.RELAXNG,
            XMLSchemaType.DTD,
        }:
            err_msg = _(
                "Invalid or missing XML schema_type '%(schema_type)s';"
                "must be 'XSD', 'RELAXNG', or 'DTD'.",
            ) % {"schema_type": schema_type or "<missing>"}
            raise ValueError(err_msg)
        return schema_type

    def _load_schema(self, schema_type: str, raw: str) -> Any:
        """
        Parse the XML schema string and return an lxml schema object.
        """
        try:
            from lxml import etree
        except Exception as e:  # pragma: no cover
            raise ImportError(_("XML validation requires lxml: ") + str(e)) from e

        if schema_type == XMLSchemaType.XSD.name:
            return etree.XMLSchema(etree.XML(raw.encode("utf-8")))
        if schema_type == XMLSchemaType.RELAXNG.name:
            return etree.RelaxNG(etree.XML(raw.encode("utf-8")))
        if schema_type == XMLSchemaType.DTD.name:
            return etree.DTD(io.StringIO(raw))
        raise ValueError(_("Unsupported XML schema type: ") + schema_type)

    # Error types that just restate a parent failure and add no useful info.
    _CASCADE_ERROR_TYPES = frozenset(
        {
            "RELAXNG_ERR_INTERSEQ",
            "RELAXNG_ERR_CONTENTVALID",
            "RELAXNG_ERR_EXTRACONTENT",
            "RELAXNG_ERR_INTEREXTRA",
            "SCHEMASV_CVC_COMPLEX_TYPE_2_4",
            "SCHEMAV_CVC_ELT_1",
        }
    )

    @classmethod
    def _is_cascade_error(cls, err) -> bool:
        """Return True for errors that cascade from a root cause."""
        type_name = getattr(err, "type_name", "") or ""
        return type_name in cls._CASCADE_ERROR_TYPES

    @staticmethod
    def _extract_error_path(err) -> str:
        """Build a human-readable path from an lxml error entry."""
        raw_path = getattr(err, "path", None) or ""
        line = getattr(err, "line", 0) or 0
        # lxml sometimes returns the string "None" instead of a real path.
        if raw_path in ("None", ""):
            raw_path = ""
        elif raw_path == "/*":
            # Root element — show a friendlier label.
            return f"(document root), line {line}" if line else "(document root)"
        if raw_path:
            return f"{raw_path} (line {line})" if line else raw_path
        if line:
            return f"line {line}"
        return ""

    def _get_schema_raw(
        self,
        *,
        validator: Validator,
        ruleset: Ruleset,
    ) -> str | None:
        raw: str | None = None
        if ruleset is not None:
            raw_candidate = getattr(ruleset, "rules", None)
            if isinstance(raw_candidate, str) and raw_candidate.strip():
                raw = raw_candidate
        if not raw and isinstance(getattr(validator, "config", None), dict):
            raw_config = validator.config.get("schema")
            if isinstance(raw_config, str):
                raw = raw_config
        return raw
