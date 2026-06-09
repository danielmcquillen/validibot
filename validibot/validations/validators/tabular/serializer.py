"""Tabular Validator step (de)serialization for workflow import/export.

Inherits the generic ruleset round-trip from :class:`StepSerializer` and
re-applies the staged Tabular assertion guards on import: row and column
expressions must match their stored stage, reference declared schema columns,
and use supported/type-compatible column aggregates. Import bypasses the form,
so these checks prevent an archive from publishing rules that can only fail at
runtime.

The column scan is shared with the form via
:func:`validibot.validations.cel_columns.referenced_row_columns`, so import and
authoring can't disagree about what counts as a reference (e.g. a column name
inside a CEL string literal is not one).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.cel_columns import referenced_column_aggregates
from validibot.validations.cel_columns import referenced_column_metrics
from validibot.validations.cel_columns import referenced_row_columns
from validibot.validations.validators.base.step_serializer import StepSerializer
from validibot.validations.validators.base.step_serializer import WorkflowImportError
from validibot.validations.validators.tabular.schema import parse_table_schema

if TYPE_CHECKING:
    from validibot.validations.models import Ruleset


class TabularStepSerializer(StepSerializer):
    """StepSerializer for the Tabular Validator with staged CEL guards."""

    def validate_imported_ruleset(
        self,
        ruleset: Ruleset,
        body: dict[str, Any],
    ) -> None:
        """Reject imported Tabular assertions that violate stage/schema rules.

        Parses the Table Schema in ``rules_text`` for the declared column names,
        then checks staged CEL expressions using the same scans the authoring
        form uses. Violations raise :class:`WorkflowImportError`.
        """
        schema = self._declared_schema(ruleset.rules_text)
        if schema is None:
            # No parseable schema -> nothing to check against. A genuinely broken
            # schema is caught when the validator runs; we don't block import on it.
            return
        declared = set(schema.field_names())
        field_types = {field.name: field.type for field in schema.fields}

        for assertion in ruleset.assertions.all():
            stage = (assertion.options or {}).get("tabular_stage", "dataset")
            expression = (assertion.rhs or {}).get("expr") or assertion.cel_cache or ""
            row_references = referenced_row_columns(expression)
            column_references = referenced_column_aggregates(expression)
            if stage == "row" and (not row_references or column_references):
                raise WorkflowImportError(
                    "Imported tabular row assertion must use row.* and cannot "
                    "use col.*.",
                    code="vaf.tabular_invalid_assertion_stage",
                )
            if stage == "column" and (not column_references or row_references):
                raise WorkflowImportError(
                    "Imported tabular column assertion must use col.* and cannot "
                    "use row.*.",
                    code="vaf.tabular_invalid_assertion_stage",
                )
            if stage == "dataset" and (row_references or column_references):
                raise WorkflowImportError(
                    "Imported tabular dataset assertion cannot use row.* or col.*.",
                    code="vaf.tabular_invalid_assertion_stage",
                )
            if stage not in {"row", "column"}:
                continue
            referenced = row_references if stage == "row" else column_references
            unknown = sorted(referenced - declared)
            if unknown:
                msg = (
                    f"Imported tabular {stage} assertion references column(s) not "
                    f"declared in the step's schema: {', '.join(unknown)}. "
                    f"Declared columns: {', '.join(sorted(declared))}."
                )
                raise WorkflowImportError(msg, code="vaf.tabular_unknown_column")
            if stage == "column":
                self._validate_column_metrics(expression, field_types)

    @staticmethod
    def _declared_schema(rules_text: str):
        """Return the parsed Table Schema, or ``None`` on failure."""
        if not rules_text:
            return None
        try:
            return parse_table_schema(json.loads(rules_text))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _validate_column_metrics(
        expression: str,
        field_types: dict[str, str],
    ) -> None:
        """Reject unsupported or type-incompatible imported aggregates."""
        valid = {
            "distinct_count",
            "null_count",
            "non_null_count",
            "null_ratio",
            "min",
            "max",
            "sum",
        }
        for column, metric in referenced_column_metrics(expression):
            if metric not in valid:
                raise WorkflowImportError(
                    f"Imported tabular column assertion uses unknown aggregate "
                    f"{metric!r}.",
                    code="vaf.tabular_unknown_aggregate",
                )
            if metric == "sum" and field_types.get(column) not in {
                "integer",
                "number",
            }:
                raise WorkflowImportError(
                    f"Imported tabular column assertion uses sum on non-numeric "
                    f"column {column!r}.",
                    code="vaf.tabular_invalid_aggregate",
                )
