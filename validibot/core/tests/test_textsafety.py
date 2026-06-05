"""Tests for ``sanitize_plain_text`` — input cleaning for stored-verbatim prose.

These cover the contract that makes the helper safe to apply to any plain-text
field: it removes characters that can break storage/rendering (NUL, other control
codes) and normalizes whitespace, while leaving *all* printable content intact.
The preservation cases matter as much as the stripping ones — they are the reason
we use this helper instead of ``strip_tags`` on fields like assertion notes, whose
whole purpose is to describe comparisons ("value <= 100", "List<int>"). A future
change that swapped in tag-stripping would fail these tests loudly.
"""

from __future__ import annotations

import pytest

from validibot.core.textsafety import sanitize_plain_text


# ── Empty / falsy input ─────────────────────────────────────────────────────
# A blank=True field stores "", so the helper must coerce all "no content" inputs
# to "" rather than leaking None onto the column.
@pytest.mark.parametrize("value", ["", None, "   ", "\n\t  "])
def test_blank_inputs_collapse_to_empty_string(value):
    """None/empty/whitespace-only input returns ``""`` for a blank=True field."""
    assert sanitize_plain_text(value) == ""


# ── Control-character / NUL stripping ───────────────────────────────────────
# These bytes are never legitimate in a note. A NUL in particular aborts a
# Postgres ``text`` insert, so it must be removed before the value is saved.
def test_strips_nul_and_control_characters():
    """NUL and C0/DEL control codes are removed; surrounding text is kept."""
    assert sanitize_plain_text("a\x00b\x07c\x1f\x7fd") == "abcd"


def test_keeps_tab_and_newline_whitespace():
    """Tab and newline are meaningful layout, not control noise, so they stay."""
    assert sanitize_plain_text("col1\tcol2\nrow2") == "col1\tcol2\nrow2"


# ── Newline normalization + trimming ────────────────────────────────────────
def test_normalizes_carriage_returns_to_newlines():
    """Windows/old-Mac line endings collapse to ``\\n`` for consistent storage."""
    assert sanitize_plain_text("a\r\nb\rc") == "a\nb\nc"


def test_trims_surrounding_whitespace_but_not_interior():
    """Leading/trailing whitespace is trimmed; internal newlines are preserved."""
    assert sanitize_plain_text("  line1\nline2  ") == "line1\nline2"


# ── Markup / comparison syntax preservation (the strip_tags trap) ───────────
# This is the crux: assertion notes routinely contain <, >, and tag-like tokens.
# strip_tags would delete these; this helper must leave them untouched, relying on
# output escaping to make them safe to display.
@pytest.mark.parametrize(
    "value",
    [
        "EUI must be <= 120 kBtu/sqft",
        "facility_electric_demand_w < 1000 and zone_area > 50",
        "expects a List<int> here",
        "placeholder <value> gets substituted",
        "<script>alert(1)</script>",  # kept verbatim; output layer escapes it
    ],
)
def test_preserves_markup_and_comparison_operators(value):
    """Printable ``<``/``>`` content survives untouched (no tag stripping)."""
    assert sanitize_plain_text(value) == value
