"""Tests for the public-page Tabular detail builder.

``build_tabular_public_details`` turns a stored Table Schema descriptor + dialect
into the submitter-facing structure rendered by the "View details" accordion on
the public workflow info page. Because that page is anonymously reachable and is
the submitter's primary reference for how to structure their CSV, two properties
matter most and are pinned here:

* **Fidelity** -- every field type, requirement, bound, allowed-value set,
  pattern, primary key and dialect option a submitter needs is carried through
  accurately, using the *same* parser the validator enforces at run time. If the
  page and the validator disagreed, submitters would build files that fail.
* **Fail-soft** -- a blank, non-JSON, or structurally invalid descriptor returns
  ``None`` (the page then omits the accordion) instead of raising, so one
  half-configured step can never 500 the public page for everyone.

The builder is a pure function over primitives, so these tests need no database.
"""

from __future__ import annotations

import json

from validibot.workflows.public_tabular import build_tabular_public_details


def _descriptor(fields: list[dict], **top) -> str:
    """Serialise a Table Schema descriptor to the JSON text the builder reads."""
    return json.dumps({"fields": fields, **top})


def _columns_by_name(details) -> dict:
    """Index a result's columns by name for targeted assertions."""
    return {col.name: col for col in details.columns}


# ── Column fidelity ──────────────────────────────────────────────────────────
# Each declared field must reach the template with its type label, requirement
# state and constraints intact -- this is the core of what a submitter reads.


def test_column_types_and_requirement_are_carried_through():
    """Type labels and required/optional state must reflect the descriptor.

    A submitter scanning the table decides what each cell should contain from
    exactly these two facts, so a regression here would silently mislead them.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [
                {"name": "id", "type": "integer", "constraints": {"required": True}},
                {"name": "note", "type": "string"},
            ],
        ),
    )

    assert details is not None
    cols = _columns_by_name(details)
    assert cols["id"].type_label == "Integer"
    assert cols["id"].required is True
    # A field with no ``required`` constraint is optional, not required.
    assert cols["note"].type_label == "Text"
    assert cols["note"].required is False


def test_enum_values_surface_as_allowed_values():
    """Enum constraints become the column's allowed-value list.

    Allowed-value columns (e.g. Darwin Core ``basisOfRecord``) are the most
    actionable guidance on the page; they must arrive as discrete values, not a
    blob, so the template can chip them.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [
                {
                    "name": "status",
                    "type": "string",
                    "constraints": {"enum": ["present", "absent"]},
                },
            ],
        ),
    )

    assert details is not None
    assert _columns_by_name(details)["status"].enum_values == ("present", "absent")


def test_numeric_bounds_render_as_inequalities_without_trailing_decimal():
    """Numeric min/max become a compact "≥ x, ≤ y" string with clean integers.

    The parser coerces bounds to floats, so this guards against a misleading
    "≥ -90.0" when the author wrote "-90" -- a small but real readability bug for
    coordinate-style fields.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [
                {
                    "name": "lat",
                    "type": "number",
                    "constraints": {"minimum": -90, "maximum": 90},
                },
            ],
        ),
    )

    assert details is not None
    assert _columns_by_name(details)["lat"].constraints == "≥ -90, ≤ 90"


def test_string_length_bounds_render_as_length_constraints():
    """minLength/maxLength surface as readable length bounds.

    String-length limits are a distinct concept from numeric ranges, so they are
    labelled "length …" to avoid a submitter mistaking a 5-char cap for a value
    of 5.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [
                {
                    "name": "code",
                    "type": "string",
                    "constraints": {"minLength": 2, "maxLength": 5},
                },
            ],
        ),
    )

    assert details is not None
    constraints = _columns_by_name(details)["code"].constraints
    assert "length ≥ 2" in constraints
    assert "length ≤ 5" in constraints


def test_pattern_is_exposed_separately_from_bounds():
    """A regex pattern is its own display field, not folded into the bounds text.

    Patterns must render as code (a regex is meaningless as prose), so the
    builder keeps ``pattern`` separate and leaves ``constraints`` empty when only
    a pattern is set.
    """
    pattern = r"^urn:lsid:marinespecies\.org:taxname:[0-9]+$"
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [{"name": "sid", "type": "string", "constraints": {"pattern": pattern}}],
        ),
    )

    assert details is not None
    col = _columns_by_name(details)["sid"]
    assert col.pattern == pattern
    assert col.constraints == ""


def test_unique_and_primary_key_flags_are_set():
    """``unique`` constraints and ``primaryKey`` membership are both flagged.

    These tell a submitter which columns must not repeat. Primary-key membership
    comes from the descriptor's top-level ``primaryKey``, not a per-field flag,
    so it is resolved independently of the column's own constraints.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [
                {"name": "occurrenceID", "type": "string"},
                {"name": "tag", "type": "string", "constraints": {"unique": True}},
            ],
            primaryKey="occurrenceID",
        ),
    )

    assert details is not None
    cols = _columns_by_name(details)
    assert cols["occurrenceID"].is_primary_key is True
    assert cols["tag"].unique is True
    assert details.primary_key == ("occurrenceID",)


def test_title_and_description_are_carried_from_raw_descriptor():
    """Author-written ``title``/``description`` enrich the column row.

    The parsed model keeps only name/type/constraints, so the builder must read
    these human hints from the raw descriptor -- they are often the clearest
    guidance a submitter gets about a column's meaning.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [
                {
                    "name": "eventDate",
                    "type": "string",
                    "title": "Event date",
                    "description": "ISO 8601 date the observation was made.",
                },
            ],
        ),
    )

    assert details is not None
    col = _columns_by_name(details)["eventDate"]
    assert col.title == "Event date"
    assert "ISO 8601" in col.description


def test_conditional_requiredness_is_surfaced():
    """The Validibot conditional-required extension reaches the row.

    "Required if X present" is a real obligation a submitter must understand, so
    the trigger field name is carried through for the template to phrase.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [
                {"name": "depth", "type": "number"},
                {
                    "name": "depthUnit",
                    "type": "string",
                    "constraints": {"x-validibot-requiredWhenPresent": "depth"},
                },
            ],
        ),
    )

    assert details is not None
    assert _columns_by_name(details)["depthUnit"].required_when == "depth"


# ── Dialect resolution ───────────────────────────────────────────────────────
# The file-level options (delimiter / encoding / header) come from the step
# config first, falling back to ruleset metadata for older/imported steps.


def test_dialect_prefers_step_config():
    """When the step config carries display dialect fields, those win.

    Newer steps store a ready-made ``delimiter_label``; using it verbatim keeps
    the public page consistent with the authoring UI.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor([{"name": "a", "type": "string"}]),
        config={"delimiter_label": "Comma", "encoding": "utf-8", "has_header": True},
    )

    assert details is not None
    assert details.dialect.delimiter_label == "Comma"
    assert details.dialect.encoding == "utf-8"
    assert details.dialect.has_header is True


def test_dialect_falls_back_to_ruleset_metadata():
    """With no config dialect fields, raw metadata is mapped to friendly labels.

    Imported steps may only have a raw ``delimiter`` in ruleset metadata; the
    builder must still produce a human label ("Semicolon") and honour a
    ``has_header`` of False so the panel stays accurate.
    """
    details = build_tabular_public_details(
        schema_text=_descriptor([{"name": "a", "type": "string"}]),
        config={},
        metadata={"delimiter": ";", "encoding": "latin-1", "has_header": False},
    )

    assert details is not None
    assert details.dialect.delimiter_label == "Semicolon"
    assert details.dialect.encoding == "latin-1"
    assert details.dialect.has_header is False


# ── Counts ───────────────────────────────────────────────────────────────────


def test_column_and_required_counts():
    """The summary counts match the columns actually produced.

    The badge "(N columns · M required)" is the page's at-a-glance summary; it
    must be derived from the real column set, not a stored count that could drift.
    """
    expected_columns = 3
    expected_required_columns = 2
    details = build_tabular_public_details(
        schema_text=_descriptor(
            [
                {"name": "a", "type": "string", "constraints": {"required": True}},
                {"name": "b", "type": "string", "constraints": {"required": True}},
                {"name": "c", "type": "string"},
            ],
        ),
    )

    assert details is not None
    assert details.column_count == expected_columns
    assert details.required_column_count == expected_required_columns


# ── Fail-soft behaviour ──────────────────────────────────────────────────────
# A public page must degrade to "no accordion", never error, on bad author data.


def test_blank_schema_returns_none():
    """Empty or whitespace-only descriptor text yields no details.

    A step whose schema was never saved should simply omit the accordion rather
    than render an empty shell.
    """
    assert build_tabular_public_details(schema_text="") is None
    assert build_tabular_public_details(schema_text="   \n  ") is None


def test_invalid_json_returns_none():
    """Non-JSON descriptor text is swallowed into a ``None`` result.

    Corrupt stored config must not raise for an anonymous visitor -- the page
    keeps working, just without the detail panel.
    """
    assert build_tabular_public_details(schema_text="{not valid json") is None


def test_descriptor_without_usable_fields_returns_none():
    """A descriptor the parser rejects (no fields) degrades to ``None``.

    ``parse_table_schema`` raises on a fields-less descriptor; the builder must
    catch that and fail soft rather than propagating to the view.
    """
    assert build_tabular_public_details(schema_text=json.dumps({})) is None
    assert build_tabular_public_details(schema_text=json.dumps({"fields": []})) is None
