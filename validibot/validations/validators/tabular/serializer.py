"""Tabular Validator step (de)serialization for workflow import/export.

Inherits the generic ruleset round-trip from :class:`StepSerializer` and adds
one validator-specific guard on import: the same "a row assertion may only
reference columns declared in the Table Schema" rule the step-editor form
enforces. Import bypasses the form, so without this an archive could create a
ruleset whose row assertions reference ``row.<undeclared>`` — which would fail
every row at runtime. Re-checking here turns that into a clear import failure.

The column scan is shared with the form via
:func:`validibot.validations.cel_columns.referenced_row_columns`, so import and
authoring can't disagree about what counts as a reference (e.g. a column name
inside a CEL string literal is not one).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.cel_columns import referenced_row_columns
from validibot.validations.validators.base.step_serializer import StepSerializer
from validibot.validations.validators.base.step_serializer import WorkflowImportError
from validibot.validations.validators.tabular.schema import parse_table_schema

if TYPE_CHECKING:
    from validibot.validations.models import Ruleset


class TabularStepSerializer(StepSerializer):
    """StepSerializer for the Tabular Validator with a row-column import guard."""

    def validate_imported_ruleset(
        self,
        ruleset: Ruleset,
        body: dict[str, Any],
    ) -> None:
        """Reject row assertions that reference columns the schema doesn't declare.

        Parses the Table Schema in ``rules_text`` for the declared column names,
        then checks every row-stage assertion's CEL expression using the same
        scan the authoring form uses. A reference to an undeclared column raises
        :class:`WorkflowImportError` (surfaced on the import error page).
        """
        declared = self._declared_columns(ruleset.rules_text)
        if not declared:
            # No parseable schema -> nothing to check against. A genuinely broken
            # schema is caught when the validator runs; we don't block import on it.
            return

        for assertion in ruleset.assertions.all():
            if (assertion.options or {}).get("tabular_stage") != "row":
                continue
            expression = (assertion.rhs or {}).get("expr") or assertion.cel_cache or ""
            unknown = sorted(referenced_row_columns(expression) - declared)
            if unknown:
                msg = (
                    f"Imported tabular row assertion references column(s) not "
                    f"declared in the step's schema: {', '.join(unknown)}. "
                    f"Declared columns: {', '.join(sorted(declared))}."
                )
                raise WorkflowImportError(msg, code="vaf.tabular_unknown_column")

    @staticmethod
    def _declared_columns(rules_text: str) -> set[str]:
        """Return the Table Schema's declared column names, or empty on failure."""
        if not rules_text:
            return set()
        try:
            schema = parse_table_schema(json.loads(rules_text))
        except (ValueError, TypeError, json.JSONDecodeError):
            return set()
        return set(schema.field_names())
