"""The Tabular Validator — ties the reader and native validation together.

``validate()`` reads the submitted CSV into the shared in-memory model, runs
native structured validation against the ruleset's Table Schema, evaluates
per-row CEL assertions (the ``row.*`` namespace, with its compiled-once-per-run
loop), maps the resulting :class:`NativeFinding`s onto the platform's
``ValidationIssue``, and runs the standard CEL assertion lane (dataset ``i.*`` +
output) for any assertions on the ruleset. It also exposes the ``i.*`` dataset
signals so a ``i.num_rows >= 100``-style assertion can resolve.

Configuration lives on the ruleset, mirroring the JSON Schema validator:

- ``ruleset.rules`` (``rules_text``/``rules_file``) holds the **Table Schema
  descriptor** (JSON) — the structured column config.
- ``ruleset.metadata`` holds the **dialect** (``delimiter``, ``has_header``,
  ``quotechar``) and ``report_max_examples``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.tabular.native import DEFAULT_REPORT_MAX_EXAMPLES
from validibot.validations.validators.tabular.native import validate_native
from validibot.validations.validators.tabular.preflight import TabularDialect
from validibot.validations.validators.tabular.preflight import TabularLimits
from validibot.validations.validators.tabular.preflight import TabularReadError
from validibot.validations.validators.tabular.readers.csv import read_csv
from validibot.validations.validators.tabular.row_eval import RowAssertion
from validibot.validations.validators.tabular.row_eval import evaluate_row_assertions
from validibot.validations.validators.tabular.schema import parse_table_schema

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Submission
    from validibot.validations.models import Validator
    from validibot.validations.validators.tabular.native import NativeFinding
    from validibot.validations.validators.tabular.readers.csv import ReadResult
    from validibot.validations.validators.tabular.schema import TabularSchema

# A schema that won't parse is a configuration error, surfaced as a finding.
CODE_INVALID_SCHEMA = "tabular.invalid_schema"
# The fallback when a ruleset doesn't pin its own ``report_max_examples``. Kept
# as an alias of the canonical native default so there is one number to change,
# and so a future per-step setting only has to write ``metadata`` — the override
# path in ``_load_settings`` already reads it.
_DEFAULT_REPORT_MAX_EXAMPLES = DEFAULT_REPORT_MAX_EXAMPLES


class TabularValidator(BaseValidator):
    """In-process validator for tabular data (CSV in V1).

    See the module docstring and ADR-2026-05-26 for the full design. The
    validate flow is: load schema → read CSV → native validation → CEL
    assertion lane, returning aggregated ``ValidationIssue``s.
    """

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)
        # Populated in validate() and returned by extract_input_signals() so
        # dataset (input-stage) CEL assertions can resolve the i.* namespace.
        self._input_signals: dict[str, Any] = {}

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Validate a tabular submission and return aggregated issues."""
        self.run_context = run_context
        self._input_signals = {}

        # 1. Load the structured config (Table Schema). A bad schema is a
        #    configuration error reported as a single finding, not a crash.
        try:
            schema = self._load_schema(ruleset)
        except (ValueError, TypeError) as exc:
            return self._single_error(
                CODE_INVALID_SCHEMA,
                str(exc),
                stats={"exception": type(exc).__name__},
            )

        dialect, limits, report_max_examples = self._load_settings(ruleset)

        # 2. Read the body. Content arrives pre-decoded from get_content(); we
        #    re-encode as UTF-8 and read as UTF-8. Encoding is pinned to UTF-8
        #    in V1 (there is no editable encoding setting) because get_content()
        #    has already decoded the submission — honoring another encoding needs
        #    a raw-bytes read path (a future slice). A read failure (oversized,
        #    ragged, undecodable) becomes a single finding carrying its code.
        content = submission.get_content() or ""
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        declared_columns = None if dialect.has_header else schema.field_names()
        try:
            read_result = read_csv(
                content_bytes,
                dialect=dialect,
                declared_columns=declared_columns,
                limits=limits,
            )
        except TabularReadError as exc:
            return self._single_error(
                exc.code,
                str(exc),
                stats={"read_error": exc.code},
            )

        # 3. Native structured validation against the schema.
        native_findings = validate_native(
            read_result,
            schema,
            report_max_examples=report_max_examples,
        )
        issues = [self._to_issue(finding) for finding in native_findings]

        # 4. Dataset signals (i.*) — exposed for input-stage CEL assertions and
        #    returned for downstream steps.
        self._input_signals = self._build_input_signals(read_result, submission)

        # 5. Row-stage CEL (the row.* loop). Validator-owned: these assertions
        #    are skipped by the generic lane (they reference row.*, which it
        #    doesn't bind) and evaluated here against every row, with now()
        #    pinned to the run clock.
        row_assertions = self._collect_row_assertions(validator, ruleset)
        row_findings = evaluate_row_assertions(
            read_result,
            schema,
            row_assertions,
            signals=self._workflow_signals(run_context),
            input_signals=self._input_signals,
            now=self._run_clock(run_context),
            report_max_examples=report_max_examples,
        )
        issues.extend(self._to_issue(finding) for finding in row_findings)

        # 6. The standard CEL assertion lane (dataset i.* + output). We pass an
        #    empty payload because i.* is supplied via extract_input_signals();
        #    row/column assertions are excluded by the lane itself.
        assertion_result = self.evaluate_assertions_for_stages(
            validator=validator,
            ruleset=ruleset,
            payload={},
        )
        issues.extend(assertion_result.issues)

        # Assertion stats count *assertions* (not rows): the generic lane's
        # totals plus the row assertions, with a row assertion counted as a
        # failure when it produced any finding.
        failed_row_assertion_ids = {
            finding.assertion_id
            for finding in row_findings
            if finding.assertion_id is not None
        }
        passed = not any(issue.severity == Severity.ERROR for issue in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=assertion_result.total + len(row_assertions),
                failures=assertion_result.failures + len(failed_row_assertion_ids),
            ),
            signals=self._input_signals,
            stats={
                "num_rows": read_result.num_rows,
                "num_columns": read_result.num_columns,
                "native_finding_count": len(native_findings),
                "row_assertion_count": len(row_assertions),
            },
        )

    def extract_input_signals(self, payload: Any) -> dict[str, Any] | None:
        """Expose the ``i.*`` dataset metadata computed during ``validate()``.

        The signals are derived from the parsed dataframe (row/column counts,
        column names, dialect), not from re-parsing *payload*, so the argument
        is ignored. Returns ``None`` when no dataset has been read yet, matching
        the base default (which leaves ``i.*`` empty).
        """
        return self._input_signals or None

    # ------------------------------------------------------------------ private

    def _single_error(
        self,
        code: str,
        message: str,
        *,
        stats: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """Build a failed result carrying one ERROR issue (config/read failure)."""
        return ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    path="",
                    message=message,
                    severity=Severity.ERROR,
                    code=code,
                ),
            ],
            stats=stats,
        )

    def _to_issue(self, finding: NativeFinding) -> ValidationIssue:
        """Map a :class:`NativeFinding` onto a platform ``ValidationIssue``.

        The richer native shape (count + sample rows + column) is preserved in
        ``meta`` so the finding row and UI can show "12 of 50 rows failed; e.g.
        rows 3, 7, 9" without the message text having to carry it.
        """
        return ValidationIssue(
            path=finding.column or "",
            message=finding.message,
            severity=finding.severity,
            code=finding.code,
            meta={
                "count": finding.count,
                "sample_rows": list(finding.sample_rows),
                "column": finding.column,
            },
            assertion_id=finding.assertion_id,
        )

    def _load_schema(self, ruleset: Ruleset) -> TabularSchema:
        raw_schema = getattr(ruleset, "rules", None)
        if not raw_schema:
            msg = _(
                "Tabular ruleset must provide a Table Schema via rules_text "
                "or rules_file.",
            )
            raise ValueError(msg)
        descriptor = (
            raw_schema if isinstance(raw_schema, dict) else json.loads(raw_schema)
        )
        return parse_table_schema(descriptor)

    def _load_settings(
        self,
        ruleset: Ruleset,
    ) -> tuple[TabularDialect, TabularLimits, int]:
        metadata = getattr(ruleset, "metadata", None) or {}
        dialect = TabularDialect(
            # None means "sniff"; an empty string in metadata also means sniff.
            delimiter=metadata.get("delimiter") or None,
            quotechar=metadata.get("quotechar", '"'),
            # Pinned to UTF-8 in V1; metadata["encoding"] is always "utf-8".
            encoding="utf-8",
            has_header=bool(metadata.get("has_header", True)),
        )
        report_max_examples = metadata.get(
            "report_max_examples",
            _DEFAULT_REPORT_MAX_EXAMPLES,
        )
        try:
            report_max_examples = int(report_max_examples)
        except (TypeError, ValueError):
            report_max_examples = _DEFAULT_REPORT_MAX_EXAMPLES
        return dialect, TabularLimits(), report_max_examples

    def _collect_row_assertions(
        self,
        validator: Validator,
        ruleset: Ruleset,
    ) -> list[RowAssertion]:
        """Gather the ruleset's row CEL assertions as engine specs.

        Row assertions are ``RulesetAssertion`` rows tagged
        ``options["tabular_stage"] == "row"`` (the persistence decision in
        ADR-2026-05-26). Both the validator's default ruleset and the step
        ruleset are scanned, matching the generic lane's source order.
        """
        specs: list[RowAssertion] = []
        for source in (getattr(validator, "default_ruleset", None), ruleset):
            if source is None:
                continue
            for assertion in source.assertions.all():
                if (assertion.options or {}).get("tabular_stage") != "row":
                    continue
                expression = (
                    (assertion.rhs or {}).get("expr") or assertion.cel_cache or ""
                )
                if not expression:
                    continue
                specs.append(
                    RowAssertion(
                        expression=expression,
                        message=assertion.message_template or "",
                        severity=Severity(assertion.severity or Severity.ERROR),
                        assertion_id=assertion.pk,
                    ),
                )
        return specs

    def _run_clock(self, run_context: RunContext | None) -> Any:
        """Return the run's ``started_at`` to pin ``now()`` (or None).

        When there is no run context (e.g. a direct unit-test call), ``now()``
        is left unbound and any assertion using it fails cleanly — never the
        wall clock.
        """
        run = getattr(run_context, "validation_run", None)
        return getattr(run, "started_at", None)

    def _workflow_signals(self, run_context: RunContext | None) -> dict[str, Any]:
        """Return the workflow signals (s.*) available to row assertions."""
        return getattr(run_context, "workflow_signals", None) or {}

    def _build_input_signals(
        self,
        read_result: ReadResult,
        submission: Submission,
    ) -> dict[str, Any]:
        filename = (
            getattr(submission, "input_filename", None)
            or getattr(submission, "original_filename", None)
            or ""
        )
        preflight = read_result.preflight
        return {
            "num_rows": read_result.num_rows,
            "num_columns": read_result.num_columns,
            "column_names": list(read_result.column_names),
            "delimiter": preflight.delimiter,
            "encoding": preflight.encoding,
            "has_header": preflight.has_header,
            "size_bytes": preflight.size_bytes,
            "filename": filename,
        }
