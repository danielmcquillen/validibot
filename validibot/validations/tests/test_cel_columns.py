"""Tests for the shared row-column CEL scan.

``referenced_row_columns`` is the single definition of "which ``row.<column>``
references does this row assertion make", used by *both* the assertion authoring
form and the workflow importer. They must agree, so the subtle cases — string
literals that merely look like references, bracket access, false-positive
substrings — are pinned here.
"""

from __future__ import annotations

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


def test_strip_removes_single_and_double_quoted_literals():
    """The literal-stripper drops quoted content (with escapes), keeps the rest."""
    assert strip_cel_string_literals(r'a + "he\"llo" + b') == "a +  + b"
    assert strip_cel_string_literals("x + 'row.y' + z") == "x +  + z"
