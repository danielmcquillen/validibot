from __future__ import annotations

import io
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.engines.base import BaseValidatorEngine
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.base import ValidationResult
from validibot.validations.engines.registry import register_engine

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator


@register_engine(ValidationType.XML_SCHEMA)
class XmlSchemaValidatorEngine(BaseValidatorEngine):
    """
    XML validator that supports XSD (default), Relax NG, and DTD.

    Ruleset requirements:
      * ``ruleset.metadata['schema_type']`` must be one of ``XMLSchemaType``.
      * ``ruleset.rules_text`` or ``ruleset.rules_file`` should provide the schema text.

    For legacy rulesets that did not embed the schema, we fall back to
    ``validator.config['schema']``. New rulesets should keep the schema in
    metadata so it travels with the reusable asset.
    """

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
        # Store run_context on instance for CEL evaluation methods
        self.run_context = run_context
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
            parser = etree.XMLParser(recover=False)
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
        if ok:
            return ValidationResult(
                passed=True,
                issues=[],
                stats={"error_count": 0, "schema_type": schema_type},
            )

        issues: list[ValidationIssue] = []
        for err in getattr(schema, "error_log", []) or []:
            path = (
                getattr(err, "path", "")
                or f"$ (line {getattr(err, 'line', '?')}, "
                "column {getattr(err, 'column', '?')})"
            )
            issues.append(ValidationIssue(path, str(err.message), Severity.ERROR))

        return ValidationResult(
            passed=False,
            issues=issues,
            stats={"error_count": len(issues), "schema_type": schema_type},
        )

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
        raise ValueError(_("Unsupported XML engine: ") + schema_type)

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
