"""Tests for the shared row-column CEL scan.

``referenced_row_columns`` is the single definition of "which ``row.<column>``
references does this row assertion make", used by *both* the assertion authoring
form and the workflow importer. They must agree, so the subtle cases — string
literals that merely look like references, bracket access, false-positive
substrings — are pinned here.
"""

from __future__ import annotations

from validibot.validations.cel_columns import bound_macro_variables
from validibot.validations.cel_columns import referenced_column_aggregates
from validibot.validations.cel_columns import referenced_column_metrics
from validibot.validations.cel_columns import referenced_row_columns
from validibot.validations.cel_columns import strip_cel_string_literals


def test_dot_and_bracket_access_are_both_found():
    """Both ``row.lat`` and ``row["dwc:eventDate"]`` spellings are references."""
    expr = "row.lat >= -90 && row[\"dwc:eventDate\"] != ''"
    assert referenced_row_columns(expr) == {"lat", "dwc:eventDate"}


def test_column_name_inside_a_string_literal_is_not_a_reference():
    """A token shaped like ``row.x`` inside a quoted string is not a real ref.

    This is the import/authoring-agreement bug: the importer used to flag
    ``"row.notAColumn"`` as a reference to an undeclared column, while the editor
    (which strips literals first) did not. Now both use this scan, so a perfectly
    valid expression like ``row.scientificName != "row.notAColumn"`` references
    only ``scientificName``.
    """
    assert referenced_row_columns('row.scientificName != "row.notAColumn"') == {
        "scientificName",
    }


def test_substring_of_another_identifier_is_not_a_reference():
    """``arrow.x`` must not be read as a ``row.x`` reference."""
    assert referenced_row_columns("arrow.x > 0 || foo.row.bar < 1") == set()


def test_bracket_reference_inside_a_string_literal_is_not_a_reference():
    """A whole ``row["x"]`` sitting inside an outer string literal is not a ref.

    The dot-access fix wasn't enough on its own: the bracket scan used to run on
    the raw expression, so ``'row["notAColumn"]'`` (the bracket access is entirely
    inside an outer single-quoted literal) was wrongly counted. The literal-aware
    pass now collapses the whole outer literal to one token, so nothing matches.
    """
    assert referenced_row_columns("""x == 'row["notAColumn"]'""") == set()


def test_real_bracket_reference_is_still_found():
    """A genuine ``row["col"]`` access is still recognised after the fix."""
    assert referenced_row_columns('row["dwc:eventDate"] != ""') == {"dwc:eventDate"}


def test_column_aggregate_references_support_dot_and_bracket_access():
    """V2 column checks identify both the column and selected aggregate.

    Authors may use readable dot aliases or canonical bracket access, and import
    validation must understand both spellings without inspecting string literals.
    """
    expression = 'col.depth.null_ratio < 0.05 && col["dwc:eventDate"].max <= now()'
    assert referenced_column_aggregates(expression) == {"depth", "dwc:eventDate"}
    assert referenced_column_metrics(expression) == {
        ("depth", "null_ratio"),
        ("dwc:eventDate", "max"),
    }


def test_column_metrics_detected_across_all_access_spellings():
    """Every dot/bracket spelling of a metric resolves, closing a validation gap.

    The old dot-only metric scan let ``col["x"]["sum"]``, ``col.x["sum"]``, and a
    space-padded dot evade the aggregate-name/type validation and fail only at
    run time. All four column×metric access combinations (and whitespace) must
    now be recognised so author-time validation sees the metric.
    """
    assert referenced_column_metrics("col.x.sum") == {("x", "sum")}
    assert referenced_column_metrics('col["x"].sum') == {("x", "sum")}
    assert referenced_column_metrics('col.x["sum"]') == {("x", "sum")}
    assert referenced_column_metrics('col["x"]["sum"]') == {("x", "sum")}
    assert referenced_column_metrics('col["x"]  .  sum') == {("x", "sum")}
    # A literal that merely looks like a reference is still ignored.
    assert referenced_column_metrics('"col.x.sum"') == set()


def test_strip_removes_single_and_double_quoted_literals():
    """The literal-stripper drops quoted content (with escapes), keeps the rest."""
    assert strip_cel_string_literals(r'a + "he\"llo" + b') == "a +  + b"
    assert strip_cel_string_literals("x + 'row.y' + z") == "x +  + z"


# ── Comprehension-macro loop variables ──────────────────────────────────────
def test_macro_loop_variable_is_found_any_length():
    """The loop variable a macro introduces is collected, even multi-letter.

    This is the fix's core: ``ns`` in ``items.all(ns, ...)`` is bound by the
    macro, so the identifier check must exempt it. The old code only recognised
    single-letter loop variables.
    """
    assert bound_macro_variables("i.items.all(ns, ns in allowed)") == {"ns"}


def test_all_comprehension_macros_and_nesting():
    """every comprehension macro binds its first arg; nested macros stack."""
    expr = "a.map(room, room.size).exists(device, device > 0)"
    assert bound_macro_variables(expr) == {"room", "device"}


def test_has_macro_binds_no_variable():
    """``has(x)`` takes a field selection, not a loop variable, so binds nothing."""
    assert bound_macro_variables("has(p.x) && i.y > 0") == set()


def test_macro_inside_a_string_literal_does_not_count():
    """A macro-looking token inside a quoted string isn't real syntax."""
    assert bound_macro_variables('p.note == "items.all(x,"') == set()
