from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.constants import XMLSchemaType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.engines.base import ValidationIssue
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.engines.registry import register_engine

if TYPE_CHECKING:
    from simplevalidations.submissions.models import Submission
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Validator


@register_engine(ValidationType.XML_SCHEMA)
class XmlSchemaValidatorEngine(BaseValidatorEngine):
    """
    XML validator that supports XSD (default) and Relax NG.

    Select engine via ruleset.metadata['engine'] or
    ruleset.config['engine'] ∈ {'XSD','RELAXNG'}.

    Provide the schema under ruleset.metadata['schema'] or ruleset.config['schema'].

    Expects a 'schema' entry in config:
      - str: the XSD schema as a string

    Example config on your Validator model:
      {
        "schema": "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>...</xs:schema>"
      }
    """

    def _resolve_schema_type(self, ruleset) -> str:
        schema_type = None
        if ruleset is not None:
            for cfg in (
                getattr(ruleset, "config", None),
                getattr(ruleset, "metadata", None),
            ):
                if isinstance(cfg, dict) and "schema_type" in cfg:
                    schema_type = (cfg["schema_type"] or "").strip().upper()
                    break
        # Expect the upper-case string of the enum's value (e.g., "XSD" or "RELAXNG")
        if schema_type not in {XMLSchemaType.XSD.value, XMLSchemaType.RELAXNG.value}:
            err_msg = _(
                "Invalid or missing XML schema_type '%(schema_type)s';"
                "must be 'XSD' or 'RELAXNG'.",
            ) % {"schema_type": schema_type or "<missing>"}
            raise ValueError(err_msg)
        return schema_type

    def _load_schema(self, schema_type: str, raw: str) -> Any:
        """
        Parse the XML schema string and return an lxml schema object.
        """
        try:
            from lxml import etree  # noqa: PLC0415
        except Exception as e:  # pragma: no cover
            raise ImportError(_("XML validation requires lxml: ") + str(e)) from e

        if schema_type == XMLSchemaType.XSD.name:
            return etree.XMLSchema(etree.XML(raw.encode("utf-8")))
        if schema_type == XMLSchemaType.RELAXNG.name:
            return etree.RelaxNG(etree.XML(raw.encode("utf-8")))
        raise ValueError(_("Unsupported XML engine: ") + schema_type)

    def _get_schema_raw(self, *, validator, ruleset) -> str | None:
        raw = None
        if ruleset is not None:
            for cfg in (
                getattr(ruleset, "config", None),
                getattr(ruleset, "metadata", None),
            ):
                if isinstance(cfg, dict) and "schema" in cfg:
                    raw = cfg["schema"]
                    break
        if raw is None and isinstance(getattr(validator, "config", None), dict):
            raw = validator.config.get("schema")
        if isinstance(raw, str):
            return raw
        return None

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
    ) -> ValidationResult:
        """
        Validate the provided XML against the configured schema (XSD or Relax NG).
        Returns a ValidationResult with ERROR issues for any schema violations.
        """
        # lxml optional (import lazily)
        try:
            from lxml import etree  # noqa: PLC0415
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
