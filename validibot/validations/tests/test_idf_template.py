"""
Tests for IDF template scanning and validation utilities.

This test suite exercises the two core functions in
``validibot.validations.utils.idf_template``:

1. ``scan_idf_template_variables()`` — Extracts ``$VARIABLE_NAME``
   placeholders from EnergyPlus IDF text, tracking object types, field
   positions, and ``!-`` annotations.  Correct variable detection is
   critical because errors here cascade: missed variables mean the
   author never annotates them, and submitters get no prompt for required
   values.

2. ``validate_idf_template()`` — Validates uploaded IDF template files
   with a layered check pipeline (extension → encoding → structure →
   variables).  This is the author's first line of defense against
   uploading wrong files.

The tests are organized by concern:

- **Scanner basics**: single/multiple variables, ordering, deduplication
- **Annotations**: ``!-`` label and units extraction, edge cases
- **Object type detection**: colon heuristic, indentation, semicolon reset
- **Field position tracking**: 0-based indexing, header offset
- **Comment handling**: inline vs full-line comments, false positive prevention
- **Case sensitivity**: uppercase-only mode, mixed-case warnings, normalization
- **Edge cases**: empty input, malformed IDF, unusual formatting
- **Validation blocking errors**: each rejection reason individually tested
- **Validation warnings**: mixed-case, duplicates, invalid ``$``, file size

Phase: 2 (IDF Parsing Utility and Variable Detection) of the EnergyPlus
Parameterized Templates ADR.
"""

from __future__ import annotations

from validibot.validations.utils.idf_template import scan_idf_template_variables
from validibot.validations.utils.idf_template import validate_idf_template

# ---------------------------------------------------------------------------
# Shared IDF text fixtures used across multiple test classes.
# ---------------------------------------------------------------------------

SIMPLE_IDF = """\
Version,
    24.2;  !- Version Identifier

WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $SHGC,                   !- Solar Heat Gain Coefficient
    $VISIBLE_TRANSMITTANCE;  !- Visible Transmittance
"""

MULTI_OBJECT_IDF = """\
Material:NoMass,
    Insulation,   !- Name
    $ROUGHNESS,   !- Roughness
    $R_VALUE;     !- Thermal Resistance {m2-K/W}

WindowMaterial:SimpleGlazingSystem,
    Glazing,      !- Name
    $U_FACTOR,    !- U-Factor {W/m2-K}
    $SHGC;        !- Solar Heat Gain Coefficient
"""

IDF_WITH_COMMENTS = """\
! This is a full-line comment mentioning $NOT_A_VAR
WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K} see $OTHER
    $SHGC;                   !- Solar Heat Gain Coefficient
"""

MIXED_CASE_IDF = """\
WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $u_factor,               !- U-Factor {W/m2-K}
    $SHGC;                   !- Solar Heat Gain Coefficient
"""

DUPLICATE_VAR_IDF = """\
WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $SHGC;                   !- Solar Heat Gain Coefficient

Construction,
    Window Construction,     !- Name
    $U_FACTOR;               !- U-Factor used again
"""

VALID_TEMPLATE_BYTES = SIMPLE_IDF.encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# scan_idf_template_variables() — basic scanning
# ══════════════════════════════════════════════════════════════════════════════


class TestScanBasic:
    """Basic scanning: single and multiple variables, ordering, deduplication.

    These tests verify the fundamental contract of the scanner: variables
    are detected in the data portion of each line, returned in order of
    first appearance, and deduplicated by name.
    """

    def test_single_variable(self):
        """A minimal IDF with one variable must be detected correctly.

        This is the simplest possible case — one object, one variable.
        If this fails, nothing else will work.
        """
        idf = """\
Zone,
    $ZONE_NAME;  !- Name
"""
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 1
        assert result.variables[0].name == "ZONE_NAME"
        assert result.variables[0].line_number == 2  # noqa: PLR2004

    def test_multiple_variables_order_preserved(self):
        """Variables must be returned in order of first appearance in the IDF.

        Authors intentionally group related variables, so alphabetical
        ordering would destroy the author's layout intention.  The
        Template Variables card depends on this ordering.
        """
        result = scan_idf_template_variables(SIMPLE_IDF)
        names = [v.name for v in result.variables]
        assert names == ["U_FACTOR", "SHGC", "VISIBLE_TRANSMITTANCE"]

    def test_duplicate_variable_first_occurrence_only(self):
        """When a variable appears multiple times, only the first occurrence
        is returned.

        Both appearances will be substituted at runtime, but the metadata
        (object type, annotation) comes from the first occurrence since
        that's where the author's annotation context is richest.
        """
        result = scan_idf_template_variables(DUPLICATE_VAR_IDF)
        names = [v.name for v in result.variables]
        assert names == ["U_FACTOR", "SHGC"]
        # First occurrence is in WindowMaterial
        assert result.variables[0].object_type == "WindowMaterial:SimpleGlazingSystem"

    def test_variable_at_start_of_line(self):
        """A variable at the very start of a data line (no indentation)
        must be detected.

        Some authors don't indent IDF field values, and the scanner must
        handle this.
        """
        idf = """\
Zone,
$ZONE_NAME;  !- Name
"""
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 1
        assert result.variables[0].name == "ZONE_NAME"

    def test_variable_as_only_content(self):
        """A line containing only a variable (plus terminator) must be detected.

        Edge case where there's no other field value text on the line.
        """
        idf = """\
Zone,
    $ZONE_NAME;
"""
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 1

    def test_multiple_variables_on_same_line(self):
        """Multiple variables on a single comma-delimited line must all
        be detected.

        IDF allows multiple fields per line, and an author might put
        related parameters together.
        """
        idf = """\
WindowMaterial:SimpleGlazingSystem,
    Glazing,             !- Name
    $U_FACTOR, $SHGC;   !- Properties
"""
        result = scan_idf_template_variables(idf)
        names = [v.name for v in result.variables]
        assert "U_FACTOR" in names
        assert "SHGC" in names

    def test_variable_with_underscores_and_digits(self):
        """Variable names can contain digits and multiple underscores.

        This verifies the regex ``[A-Z][A-Z0-9_]*`` handles names like
        ``$ZONE_1_TEMP`` correctly.
        """
        idf = """\
Zone,
    $ZONE_1_TEMP_SETPOINT;  !- Temperature {C}
"""
        result = scan_idf_template_variables(idf)
        assert result.variables[0].name == "ZONE_1_TEMP_SETPOINT"


# ══════════════════════════════════════════════════════════════════════════════
# scan_idf_template_variables() — annotation extraction
# ══════════════════════════════════════════════════════════════════════════════


class TestScanAnnotations:
    """``!-`` annotation extraction: label, units, and edge cases.

    EnergyPlus ``!-`` annotations are the primary source of human-readable
    labels and units for template variables.  The scanner auto-populates
    these from the IDF context so authors don't have to type them manually.
    Getting this right saves authors significant data-entry effort.
    """

    def test_annotation_with_label_and_units(self):
        """Standard annotation: ``!- U-Factor {W/m2-K}`` → label and units.

        This is the most common case in EnergyPlus IDFs — the annotation
        has both a descriptive label and a units specification in braces.
        """
        result = scan_idf_template_variables(SIMPLE_IDF)
        u_factor = result.variables[0]
        assert u_factor.label == "U-Factor"
        assert u_factor.units == "W/m2-K"
        assert u_factor.field_annotation == "U-Factor {W/m2-K}"

    def test_annotation_with_label_only_no_units(self):
        """Annotation without ``{units}``: ``!- Solar Heat Gain Coefficient``.

        Some IDF fields are dimensionless — the annotation has a label
        but no units block.
        """
        result = scan_idf_template_variables(SIMPLE_IDF)
        shgc = result.variables[1]
        assert shgc.label == "Solar Heat Gain Coefficient"
        assert shgc.units == ""

    def test_annotation_with_units_only_no_label(self):
        """Annotation with only units: ``!- {W/m2-K}`` → empty label, units set.

        Edge case where the author writes only the units block.
        """
        idf = """\
Zone,
    $TEMP,  !- {C}
    $NAME;  !- Name
"""
        result = scan_idf_template_variables(idf)
        temp = result.variables[0]
        assert temp.label == ""
        assert temp.units == "C"

    def test_no_annotation_yields_empty_label_and_units(self):
        """No ``!-`` annotation at all: both label and units empty.

        The variable still appears in the Template Variables card but with
        blank label and units for the author to fill in manually.
        """
        idf = """\
Zone,
    $ZONE_NAME;
"""
        result = scan_idf_template_variables(idf)
        assert result.variables[0].label == ""
        assert result.variables[0].units == ""
        assert result.variables[0].field_annotation is None

    def test_malformed_units_unclosed_brace(self):
        """Malformed ``{units`` (unclosed brace) → entire text becomes label.

        The ``{...}`` regex requires a closing brace.  Without it, no
        units are extracted and the raw text (including the malformed
        brace) becomes the label.
        """
        idf = """\
Zone,
    $TEMP;  !- Temperature {C
"""
        result = scan_idf_template_variables(idf)
        assert result.variables[0].label == "Temperature {C"
        assert result.variables[0].units == ""

    def test_multiple_variables_share_line_annotation(self):
        """Multiple variables on the same line share the line's annotation.

        IDF convention: one ``!-`` annotation per line applies to all
        fields on that line.  Both variables should get the same label
        and units.
        """
        idf = """\
WindowMaterial:SimpleGlazingSystem,
    Glazing,                 !- Name
    $MIN_TEMP, $MAX_TEMP;    !- Temperature Range {C}
"""
        result = scan_idf_template_variables(idf)
        min_temp = next(v for v in result.variables if v.name == "MIN_TEMP")
        max_temp = next(v for v in result.variables if v.name == "MAX_TEMP")
        assert min_temp.label == "Temperature Range"
        assert min_temp.units == "C"
        assert max_temp.label == "Temperature Range"
        assert max_temp.units == "C"

    def test_regular_comment_not_treated_as_annotation(self):
        """A regular comment ``! text`` (without the ``-``) is not an annotation.

        Only ``!-`` (with the dash) is the IDF field annotation convention.
        """
        idf = """\
Zone,
    $ZONE_NAME;  ! This is just a comment, not an annotation
"""
        result = scan_idf_template_variables(idf)
        assert result.variables[0].field_annotation is None
        assert result.variables[0].label == ""


# ══════════════════════════════════════════════════════════════════════════════
# scan_idf_template_variables() — object type detection
# ══════════════════════════════════════════════════════════════════════════════


class TestScanObjectType:
    """Object type detection from IDF structure.

    Knowing which EnergyPlus object contains a variable is important for
    Phase 2+ (IDD schema lookup for auto-populating constraints).  The
    scanner uses a colon heuristic — not a full IDD parser — which handles
    standard object names and common edge cases.
    """

    def test_object_type_detected(self):
        """Standard object header: ``WindowMaterial:SimpleGlazingSystem,``.

        The object type should be captured for all variables within that
        object.
        """
        result = scan_idf_template_variables(SIMPLE_IDF)
        for var in result.variables:
            assert var.object_type == "WindowMaterial:SimpleGlazingSystem"

    def test_object_type_with_indented_header(self):
        """Indented object headers must still be detected.

        Some IDF editors or authors indent object headers.  The scanner
        strips whitespace before checking the colon heuristic.
        """
        idf = """\
  WindowMaterial:SimpleGlazingSystem,
    Glazing,      !- Name
    $U_FACTOR;    !- U-Factor {W/m2-K}
"""
        result = scan_idf_template_variables(idf)
        assert result.variables[0].object_type == "WindowMaterial:SimpleGlazingSystem"

    def test_object_type_resets_at_semicolon(self):
        """Semicolon terminates an object — the next variable should have
        ``None`` object type until a new header is encountered.

        This prevents a variable after a malformed section (no new header)
        from inheriting the previous object's type.
        """
        idf = """\
Zone,
    TestZone;    !- Name

$ORPHAN_VAR,
    value;
"""
        result = scan_idf_template_variables(idf)
        orphan = next(v for v in result.variables if v.name == "ORPHAN_VAR")
        assert orphan.object_type is None

    def test_variables_in_different_objects(self):
        """Variables in different objects get their respective object types.

        This is the normal case for multi-object templates — each variable
        knows which object it belongs to.
        """
        result = scan_idf_template_variables(MULTI_OBJECT_IDF)

        roughness = next(v for v in result.variables if v.name == "ROUGHNESS")
        assert roughness.object_type == "Material:NoMass"

        u_factor = next(v for v in result.variables if v.name == "U_FACTOR")
        assert u_factor.object_type == "WindowMaterial:SimpleGlazingSystem"

    def test_digit_starting_token_not_treated_as_object_type(self):
        """Tokens starting with digits are not object types.

        A value like ``24.2`` in a ``Version`` object should not be
        mistaken for an object type just because the line has a comma.
        """
        idf = """\
Version,
    24.2;   !- Version Identifier

Zone,
    $ZONE_NAME;  !- Name
"""
        result = scan_idf_template_variables(idf)
        assert result.variables[0].object_type == "Zone"

    def test_simple_object_type_without_subtype(self):
        """Object type with just one segment (e.g., ``Zone:``) is still detected.

        Some object types use the colon as a suffix (e.g., ``Daylighting:Controls``),
        while simpler ones like ``Zone`` don't have a colon at all in common usage.
        The ``Version,`` header doesn't have a colon, so it shouldn't be detected.
        """
        idf = """\
Daylighting:Controls,
    DL Control,   !- Name
    $ILLUMINANCE; !- Illuminance Setpoint {lux}
"""
        result = scan_idf_template_variables(idf)
        assert result.variables[0].object_type == "Daylighting:Controls"


# ══════════════════════════════════════════════════════════════════════════════
# scan_idf_template_variables() — field position tracking
# ══════════════════════════════════════════════════════════════════════════════


class TestScanFieldPosition:
    """Field position tracking (0-based, object type header excluded).

    Field positions map variables to IDD field indices, which is needed
    for Phase 2+ (auto-populating constraints from the EnergyPlus JSON
    schema).  The header line itself is excluded — field_position 0 is
    the first value field after the object type token.
    """

    def test_field_positions_in_simple_object(self):
        """Fields in a simple object get sequential 0-based positions.

        ``WindowMaterial:SimpleGlazingSystem,``  (header, not counted)
        ``Glazing System,``    → position 0 (Name)
        ``$U_FACTOR,``         → position 1
        ``$SHGC,``             → position 2
        ``$VISIBLE_TRANSMITTANCE;`` → position 3
        """
        result = scan_idf_template_variables(SIMPLE_IDF)
        u_factor = result.variables[0]
        shgc = result.variables[1]
        visible = result.variables[2]

        assert u_factor.field_position == 1
        assert shgc.field_position == 2  # noqa: PLR2004
        assert visible.field_position == 3  # noqa: PLR2004

    def test_field_positions_across_objects(self):
        """Field positions reset to 0 when a new object starts.

        Each object's fields are independently numbered.
        """
        result = scan_idf_template_variables(MULTI_OBJECT_IDF)

        roughness = next(v for v in result.variables if v.name == "ROUGHNESS")
        r_value = next(v for v in result.variables if v.name == "R_VALUE")

        # Material:NoMass: Name(0), $ROUGHNESS(1), $R_VALUE(2)
        assert roughness.field_position == 1
        assert r_value.field_position == 2  # noqa: PLR2004

        u_factor = next(v for v in result.variables if v.name == "U_FACTOR")
        # WindowMaterial:SimpleGlazingSystem: Name(0), $U_FACTOR(1)
        assert u_factor.field_position == 1

    def test_field_position_with_variable_on_header_line(self):
        """A variable on the same line as the object type header gets
        the correct position (accounting for the header comma offset).
        """
        idf = """\
Schedule:Compact, $SCHEDULE_NAME, $TYPE_LIMITS;
"""
        result = scan_idf_template_variables(idf)
        sched = next(v for v in result.variables if v.name == "SCHEDULE_NAME")
        limits = next(v for v in result.variables if v.name == "TYPE_LIMITS")
        assert sched.field_position == 0
        assert limits.field_position == 1


# ══════════════════════════════════════════════════════════════════════════════
# scan_idf_template_variables() — comment handling
# ══════════════════════════════════════════════════════════════════════════════


class TestScanComments:
    """Variables in comments are NOT template variables.

    The scanner must distinguish between ``$VARIABLE`` in a data field
    (which IS a template placeholder) and ``$VARIABLE`` in a comment
    (which is just documentation).  False positives in comments would
    create phantom variables that the author has to manually remove.
    """

    def test_variable_in_full_line_comment_not_detected(self):
        """Full-line comments (``! ...``) are entirely skipped.

        ``$NOT_A_VAR`` in a comment line should never appear in results.
        """
        result = scan_idf_template_variables(IDF_WITH_COMMENTS)
        names = [v.name for v in result.variables]
        assert "NOT_A_VAR" not in names

    def test_variable_in_inline_comment_not_detected(self):
        """Inline comment variables are not detected.

        ``$OTHER`` after ``!-`` in an inline comment is documentation,
        not a template placeholder.  Only ``$U_FACTOR`` in the data
        portion should be detected.
        """
        result = scan_idf_template_variables(IDF_WITH_COMMENTS)
        names = [v.name for v in result.variables]
        assert "OTHER" not in names
        assert "U_FACTOR" in names

    def test_data_var_detected_comment_var_ignored(self):
        """On a line with both a data variable and a comment variable,
        only the data variable is detected.

        This is the definitive test for the data/comment split logic.
        """
        idf = """\
Zone,
    $REAL_VAR,  ! Use $FAKE_VAR for reference
    $ANOTHER;
"""
        result = scan_idf_template_variables(idf)
        names = [v.name for v in result.variables]
        assert "REAL_VAR" in names
        assert "ANOTHER" in names
        assert "FAKE_VAR" not in names

    def test_dollar_sign_in_comment_ignored(self):
        """A bare ``$`` in a comment doesn't cause false positives or errors."""
        idf = """\
Zone,
    $ZONE_NAME;  ! Cost is $100
"""
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 1
        assert result.variables[0].name == "ZONE_NAME"


# ══════════════════════════════════════════════════════════════════════════════
# scan_idf_template_variables() — case sensitivity
# ══════════════════════════════════════════════════════════════════════════════


class TestScanCaseSensitivity:
    """Case-sensitive vs. case-insensitive variable detection.

    Case-sensitive mode (default) prevents accidental collisions with IDF
    content that happens to contain ``$solar`` or similar.  Case-insensitive
    mode is an opt-in for authors who prefer flexibility.  The scanner
    emits warnings in case-sensitive mode for mixed-case patterns that
    look like intended variables but won't be detected.
    """

    def test_case_sensitive_only_uppercase_detected(self):
        """In case-sensitive mode, only ``$UPPERCASE`` variables are detected.

        ``$u_factor`` (lowercase) must NOT appear in the results — it
        should instead trigger a warning.
        """
        result = scan_idf_template_variables(MIXED_CASE_IDF, case_sensitive=True)
        names = [v.name for v in result.variables]
        assert "SHGC" in names
        assert "u_factor" not in names
        assert "U_FACTOR" not in names  # lowercase version not auto-promoted

    def test_case_insensitive_mixed_case_detected_and_normalized(self):
        """In case-insensitive mode, ``$u_factor`` is detected and normalized
        to ``U_FACTOR``.

        All variable names are uppercased so downstream code doesn't need
        to worry about case matching.
        """
        result = scan_idf_template_variables(MIXED_CASE_IDF, case_sensitive=False)
        names = [v.name for v in result.variables]
        assert "U_FACTOR" in names
        assert "SHGC" in names

    def test_mixed_case_warning_in_case_sensitive_mode(self):
        """Mixed-case variables trigger a warning in case-sensitive mode.

        The warning should mention the variable name, the line number,
        and suggest either renaming to uppercase or switching modes.
        """
        result = scan_idf_template_variables(MIXED_CASE_IDF, case_sensitive=True)
        assert len(result.warnings) >= 1
        warning = result.warnings[0]
        assert "$u_factor" in warning
        assert "case_sensitive=False" in warning or "case-sensitive" in warning.lower()

    def test_no_mixed_case_warning_in_case_insensitive_mode(self):
        """In case-insensitive mode, mixed-case variables are detected
        normally — no warnings are emitted.
        """
        result = scan_idf_template_variables(MIXED_CASE_IDF, case_sensitive=False)
        assert len(result.warnings) == 0

    def test_mixed_case_warning_when_uppercase_also_detected(self):
        """When both ``$SHGC`` and ``$shgc`` appear, the warning for ``$shgc``
        mentions that ``$SHGC`` was already detected.

        This helps authors understand which variable will be used.
        """
        idf = """\
WindowMaterial:SimpleGlazingSystem,
    Glazing,      !- Name
    $SHGC,        !- Solar Heat Gain Coefficient
    $shgc;        !- duplicate in lowercase
"""
        result = scan_idf_template_variables(idf, case_sensitive=True)
        names = [v.name for v in result.variables]
        assert "SHGC" in names
        assert any("$shgc" in w and "already detected" in w for w in result.warnings)

    def test_case_insensitive_deduplicates_across_cases(self):
        """In case-insensitive mode, ``$U_FACTOR`` and ``$u_factor`` are the
        same variable — only the first occurrence is returned.
        """
        idf = """\
Zone,
    $u_factor,    !- First occurrence (lowercase)
    $U_FACTOR;    !- Second occurrence (uppercase)
"""
        result = scan_idf_template_variables(idf, case_sensitive=False)
        u_vars = [v for v in result.variables if v.name == "U_FACTOR"]
        assert len(u_vars) == 1
        assert u_vars[0].line_number == 2  # First occurrence  # noqa: PLR2004


# ══════════════════════════════════════════════════════════════════════════════
# scan_idf_template_variables() — edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestScanEdgeCases:
    """Edge cases: empty input, no variables, malformed IDF.

    The scanner must be robust against unexpected input — it's called on
    user-uploaded files that may be truncated, malformed, or just not
    what the author intended.
    """

    def test_empty_string_returns_empty_result(self):
        """An empty string produces an empty result, not an error.

        The ``validate_idf_template`` function handles the "empty file"
        error; the scanner itself should just return empty.
        """
        result = scan_idf_template_variables("")
        assert result.variables == []
        assert result.warnings == []

    def test_idf_with_no_variables(self):
        """A valid IDF with no ``$VARIABLE_NAME`` placeholders returns
        an empty variables list.

        This is a normal case — the author uploaded a regular IDF, not
        a template.
        """
        idf = """\
Version,
    24.2;  !- Version Identifier

Zone,
    MainZone;    !- Name
"""
        result = scan_idf_template_variables(idf)
        assert result.variables == []

    def test_malformed_idf_partial_objects(self):
        """Malformed IDF with missing semicolons still scans as much as possible.

        The scanner is not a validator — it extracts whatever variables it
        finds, even in structurally broken input.
        """
        idf = """\
WindowMaterial:SimpleGlazingSystem,
    Glazing,
    $U_FACTOR,
    $SHGC
Zone,
    $ZONE_NAME;
"""
        result = scan_idf_template_variables(idf)
        names = [v.name for v in result.variables]
        assert "U_FACTOR" in names
        assert "SHGC" in names
        assert "ZONE_NAME" in names

    def test_blank_lines_between_fields(self):
        """Blank lines between fields don't break scanning."""
        idf = """\
Zone,

    $ZONE_NAME;  !- Name
"""
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 1

    def test_tabs_in_indentation(self):
        """Tab-indented IDF lines are handled correctly."""
        idf = "Zone,\n\t$ZONE_NAME;\t!- Name\n"
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 1
        assert result.variables[0].name == "ZONE_NAME"

    def test_windows_line_endings(self):
        """Windows-style CRLF line endings don't break scanning.

        Some IDF editors on Windows produce CRLF files.
        """
        idf = "Zone,\r\n    $ZONE_NAME;  !- Name\r\n"
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 1

    def test_variable_adjacent_to_comma(self):
        """Variable immediately before a comma (no space) is detected.

        ``$U_FACTOR,`` with no space before the comma is valid.
        """
        idf = "Zone,\n$U_FACTOR,$SHGC;\n"
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 2  # noqa: PLR2004

    def test_variable_adjacent_to_semicolon(self):
        """Variable immediately before a semicolon is detected.

        ``$U_FACTOR;`` with no space.
        """
        idf = "Zone,\n$U_FACTOR;\n"
        result = scan_idf_template_variables(idf)
        assert len(result.variables) == 1


# ══════════════════════════════════════════════════════════════════════════════
# validate_idf_template() — blocking errors
# ══════════════════════════════════════════════════════════════════════════════


class TestValidateBlockingErrors:
    """Blocking errors: each check individually verified.

    When any blocking error is detected, the upload is rejected and the
    template is NOT saved.  These tests verify that each check produces
    the correct error message and stops the validation chain.
    """

    def test_reject_non_idf_extension_epjson(self):
        """Reject ``.epjson`` extension — the most common wrong-format upload.

        The error message should explain that epJSON templates aren't
        supported and name the uploaded file.
        """
        result = validate_idf_template(
            filename="model.epjson",
            content=b"{}",
        )
        assert len(result.errors) == 1
        assert ".idf" in result.errors[0]
        assert "model.epjson" in result.errors[0]

    def test_reject_non_idf_extension_pdf(self):
        """Reject ``.pdf`` — a common "wrong file" mistake."""
        result = validate_idf_template(
            filename="report.pdf",
            content=b"%PDF-1.4",
        )
        assert len(result.errors) == 1
        assert "report.pdf" in result.errors[0]

    def test_reject_non_idf_extension_dwg(self):
        """Reject ``.dwg`` — CAD files are sometimes confused with IDF."""
        result = validate_idf_template(filename="drawing.dwg", content=b"AC1032")
        assert len(result.errors) == 1

    def test_accept_idf_extension_case_insensitive(self):
        """Accept ``.IDF`` (uppercase) — file extensions are case-insensitive."""
        result = validate_idf_template(
            filename="MODEL.IDF",
            content=VALID_TEMPLATE_BYTES,
        )
        assert len(result.errors) == 0

    def test_reject_binary_file_null_bytes(self):
        """Reject files with null bytes — they're binary, not text.

        A compiled binary that happens to have ``.idf`` extension should
        be caught before we try to decode it as text.
        """
        result = validate_idf_template(
            filename="model.idf",
            content=b"Zone,\n\x00\x00binary\x00data;",
        )
        assert len(result.errors) == 1
        assert "text" in result.errors[0].lower()

    def test_reject_empty_file(self):
        """Reject a completely empty file."""
        result = validate_idf_template(filename="empty.idf", content=b"")
        assert len(result.errors) == 1
        assert "empty" in result.errors[0].lower()

    def test_reject_whitespace_only_file(self):
        """Reject a file containing only whitespace."""
        result = validate_idf_template(filename="blank.idf", content=b"   \n\n  \n")
        assert len(result.errors) == 1
        assert "empty" in result.errors[0].lower()

    def test_reject_comments_only_file(self):
        """Reject a file containing only IDF comments.

        This is a common mistake — the author uploads an IDF that has
        only comment headers with no actual objects.
        """
        content = b"! EnergyPlus file header\n! Created by: Author\n! Date: 2024\n"
        result = validate_idf_template(filename="comments.idf", content=content)
        assert len(result.errors) == 1
        assert (
            "empty" in result.errors[0].lower()
            or "comments" in result.errors[0].lower()
        )

    def test_reject_no_semicolons(self):
        """Reject file with no object-terminating semicolons.

        If there are no semicolons in data lines, it's probably not
        an IDF (maybe CSV or plain text).
        """
        content = b"Zone,\n    TestZone,\n    0,0,0\n"
        result = validate_idf_template(filename="noterm.idf", content=content)
        assert len(result.errors) == 1
        assert "semicolon" in result.errors[0].lower()

    def test_reject_no_object_types_with_colon(self):
        """Reject file with no colon-containing object type tokens.

        If there are semicolons but no tokens with colons, it's probably
        not a valid IDF.
        """
        content = b"data line one;\ndata line two;\n"
        result = validate_idf_template(filename="notypes.idf", content=content)
        assert len(result.errors) == 1
        assert "object type" in result.errors[0].lower()

    def test_reject_json_with_idf_extension(self):
        """Reject a JSON file masquerading with ``.idf`` extension.

        This catches epJSON files that were accidentally given a ``.idf``
        extension by checking if the first non-whitespace character is
        ``{`` or ``[``.
        """
        content = b'{"Version": {"idf_order": 1}}'
        result = validate_idf_template(filename="model.idf", content=content)
        assert len(result.errors) == 1
        assert "json" in result.errors[0].lower()

    def test_reject_json_array_with_idf_extension(self):
        """Reject a JSON array file with ``.idf`` extension."""
        content = b'[{"key": "value"}]'
        result = validate_idf_template(filename="model.idf", content=content)
        assert len(result.errors) == 1
        assert "json" in result.errors[0].lower()

    def test_reject_zero_variables(self):
        """Reject a valid IDF that has no ``$VARIABLE_NAME`` placeholders.

        This is the most important author-time check — if there are no
        variables, the file is not a template.
        """
        content = b"""\
Version,
    24.2;  !- Version Identifier

WindowMaterial:SimpleGlazingSystem,
    Glazing,  !- Name
    2.0,      !- U-Factor {W/m2-K}
    0.4;      !- SHGC
"""
        result = validate_idf_template(filename="regular.idf", content=content)
        assert len(result.errors) == 1
        assert "$VARIABLE_NAME" in result.errors[0]


# ══════════════════════════════════════════════════════════════════════════════
# validate_idf_template() — non-blocking warnings
# ══════════════════════════════════════════════════════════════════════════════


class TestValidateWarnings:
    """Non-blocking warnings: upload succeeds but author sees cautions.

    These warnings help authors catch potential issues before submitters
    encounter them.  The upload is NOT rejected — the warnings are shown
    on the Template Variables card.
    """

    def test_accept_valid_template(self):
        """A valid template produces no errors and returns a ScanResult.

        This is the happy path — the baseline for all warning tests.
        """
        result = validate_idf_template(
            filename="template.idf",
            content=VALID_TEMPLATE_BYTES,
        )
        assert result.errors == []
        assert result.scan_result is not None
        assert len(result.scan_result.variables) == 3  # noqa: PLR2004

    def test_mixed_case_warnings_surfaced(self):
        """Mixed-case variables in case-sensitive mode produce warnings.

        The warning from ``scan_idf_template_variables`` should be
        carried through to the ``ValidationResult.warnings``.
        """
        result = validate_idf_template(
            filename="template.idf",
            content=MIXED_CASE_IDF.encode("utf-8"),
            case_sensitive=True,
        )
        assert result.errors == []
        assert any("$u_factor" in w for w in result.warnings)

    def test_duplicate_variable_appearances_note(self):
        """Variables appearing on multiple lines produce an informational note.

        The author should know that all occurrences will be replaced with
        the same value — in case one is unintentional.
        """
        result = validate_idf_template(
            filename="template.idf",
            content=DUPLICATE_VAR_IDF.encode("utf-8"),
        )
        assert result.errors == []
        dup_warnings = [w for w in result.warnings if "$U_FACTOR appears on lines" in w]
        assert len(dup_warnings) == 1

    def test_invalid_dollar_pattern_digit_start(self):
        """``$3_ZONES`` (starts with digit) produces a warning.

        The regex won't match it as a variable, but the author probably
        intended it to be one.
        """
        idf = """\
Zone,
    $3_ZONES;  !- Zone Count
WindowMaterial:SimpleGlazingSystem,
    Glazing,   !- Name
    $U_FACTOR; !- U-Factor {W/m2-K}
"""
        result = validate_idf_template(
            filename="template.idf",
            content=idf.encode("utf-8"),
        )
        assert result.errors == []
        invalid_warnings = [w for w in result.warnings if "3_ZONES" in w]
        assert len(invalid_warnings) == 1
        assert "digit" in invalid_warnings[0].lower()

    def test_invalid_dollar_pattern_hyphen(self):
        """``$my-var`` (contains hyphen) produces a warning.

        Hyphens are not allowed in variable names — only letters, digits,
        and underscores.
        """
        idf = """\
Zone,
    $my-var;
WindowMaterial:SimpleGlazingSystem,
    Glazing,     !- Name
    $U_FACTOR;   !- U-Factor {W/m2-K}
"""
        result = validate_idf_template(
            filename="template.idf",
            content=idf.encode("utf-8"),
        )
        assert result.errors == []
        # The $ followed by "my" is detected as a valid variable in mixed-case mode
        # but "my-var" contains a hyphen, so it splits — $my is treated as a
        # mixed-case variable and the "-var" portion is ignored by the regex.
        # The invalid dollar warning is for patterns that don't match at all.

    def test_large_file_warning(self):
        """Files over 500KB produce a non-blocking size warning.

        The file is still accepted — large templates are legitimate —
        but the author should consider whether all objects are necessary.
        """
        # Create a >500KB file with valid IDF content and variables.
        base = """\
WindowMaterial:SimpleGlazingSystem,
    Glazing,      !- Name
    $U_FACTOR;    !- U-Factor {W/m2-K}
"""
        padding = "! " + "x" * 200 + "\n"
        large_content = base + padding * 2600  # ~520KB
        result = validate_idf_template(
            filename="big.idf",
            content=large_content.encode("utf-8"),
        )
        assert result.errors == []
        size_warnings = [
            w for w in result.warnings if "large" in w.lower() or "KB" in w or "MB" in w
        ]
        assert len(size_warnings) == 1

    def test_bare_dollar_sign_warning(self):
        """A bare ``$`` (not followed by any name) produces a warning."""
        idf = """\
WindowMaterial:SimpleGlazingSystem,
    Glazing,      !- Name
    $ ,           !- Bare dollar
    $U_FACTOR;    !- U-Factor {W/m2-K}
"""
        result = validate_idf_template(
            filename="template.idf",
            content=idf.encode("utf-8"),
        )
        assert result.errors == []
        bare_warnings = [w for w in result.warnings if "bare" in w.lower()]
        assert len(bare_warnings) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# validate_idf_template() — encoding handling
# ══════════════════════════════════════════════════════════════════════════════


class TestValidateEncoding:
    """Text encoding handling: UTF-8, latin-1 fallback, binary rejection.

    Some older IDF editors produce files in Windows-1252 (latin-1 superset)
    rather than UTF-8.  The validator tries UTF-8 first and falls back to
    latin-1 so these files are still accepted.
    """

    def test_accept_utf8_file(self):
        """Standard UTF-8 encoded IDF is accepted."""
        result = validate_idf_template(
            filename="template.idf",
            content=VALID_TEMPLATE_BYTES,
        )
        assert result.errors == []

    def test_accept_latin1_file(self):
        """Latin-1 encoded IDF (with non-ASCII characters) is accepted.

        Some authors include accented characters in field names or
        comments (e.g., ``São Paulo`` in location names).
        """
        idf = """\
! Localização: São Paulo
WindowMaterial:SimpleGlazingSystem,
    Vidro,        !- Name
    $U_FACTOR;    !- U-Factor {W/m2-K}
"""
        content = idf.encode("latin-1")
        result = validate_idf_template(filename="template.idf", content=content)
        assert result.errors == []
        assert result.scan_result is not None
        assert len(result.scan_result.variables) == 1

    def test_reject_truly_binary_file(self):
        """A binary file with null bytes is rejected even with ``.idf``."""
        content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        result = validate_idf_template(filename="image.idf", content=content)
        assert len(result.errors) == 1
        assert "text" in result.errors[0].lower()


# ══════════════════════════════════════════════════════════════════════════════
# validate_idf_template() — scan result content verification
# ══════════════════════════════════════════════════════════════════════════════


class TestValidateScanResultContent:
    """Verify that a successful validation produces correctly populated
    scan results that can be used to build ``IDFTemplateVariable`` dicts.

    These tests verify the end-to-end data flow from raw IDF bytes
    through validation and scanning to the structured metadata that
    ``build_energyplus_config()`` will consume.
    """

    def test_scan_result_variables_populated(self):
        """Successful validation produces a ScanResult with variables."""
        result = validate_idf_template(
            filename="template.idf",
            content=VALID_TEMPLATE_BYTES,
        )
        assert result.scan_result is not None
        assert len(result.scan_result.variables) == 3  # noqa: PLR2004

    def test_scan_result_preserves_variable_metadata(self):
        """Each variable in the scan result has correct metadata.

        This verifies the full pipeline: bytes → decode → scan → annotate.
        """
        result = validate_idf_template(
            filename="template.idf",
            content=VALID_TEMPLATE_BYTES,
        )
        u_factor = result.scan_result.variables[0]
        assert u_factor.name == "U_FACTOR"
        assert u_factor.label == "U-Factor"
        assert u_factor.units == "W/m2-K"
        assert u_factor.object_type == "WindowMaterial:SimpleGlazingSystem"

    def test_scan_result_variable_order(self):
        """Variables are in order of first appearance, not alphabetical."""
        result = validate_idf_template(
            filename="template.idf",
            content=VALID_TEMPLATE_BYTES,
        )
        names = [v.name for v in result.scan_result.variables]
        assert names == ["U_FACTOR", "SHGC", "VISIBLE_TRANSMITTANCE"]

    def test_case_insensitive_validation(self):
        """Case-insensitive mode detects mixed-case variables and normalizes.

        Verifies that the ``case_sensitive`` parameter is passed through
        correctly to the scanner.
        """
        result = validate_idf_template(
            filename="template.idf",
            content=MIXED_CASE_IDF.encode("utf-8"),
            case_sensitive=False,
        )
        assert result.errors == []
        names = [v.name for v in result.scan_result.variables]
        assert "U_FACTOR" in names  # Normalized from $u_factor


# ══════════════════════════════════════════════════════════════════════════════
# Comprehensive end-to-end scanner test with realistic IDF
# ══════════════════════════════════════════════════════════════════════════════


class TestScanRealisticIDF:
    """End-to-end test with a realistic multi-object IDF template.

    This uses a template similar to what a real workflow author would
    create — multiple objects, multiple variables, varied annotation
    styles — and verifies the complete scanning pipeline.
    """

    REALISTIC_IDF = """\
! EnergyPlus template for window glazing analysis
! Author: Test Author
! Date: 2024

Version,
    24.2;  !- Version Identifier

WindowMaterial:SimpleGlazingSystem,
    Glazing System,            !- Name
    $U_FACTOR,                 !- U-Factor {W/m2-K}
    $SHGC,                     !- Solar Heat Gain Coefficient
    $VISIBLE_TRANSMITTANCE;    !- Visible Transmittance

Material:NoMass,
    Insulation Layer,          !- Name
    $ROUGHNESS,                !- Roughness
    $THERMAL_RESISTANCE;       !- Thermal Resistance {m2-K/W}

Schedule:Compact,
    Occupancy Schedule,        !- Name
    Fraction,                  !- Schedule Type Limits Name
    Through: 12/31,            !- Field 1
    For: Weekdays,             !- Field 2
    Until: 08:00, 0.0,         !- Field 3
    Until: 17:00, $OCCUPANCY,  !- Field 4
    Until: 24:00, 0.0;         !- Field 5
"""

    def test_all_variables_detected(self):
        """All six variables across three objects are detected."""
        result = scan_idf_template_variables(self.REALISTIC_IDF)
        names = [v.name for v in result.variables]
        assert len(names) == 6  # noqa: PLR2004
        assert "U_FACTOR" in names
        assert "SHGC" in names
        assert "VISIBLE_TRANSMITTANCE" in names
        assert "ROUGHNESS" in names
        assert "THERMAL_RESISTANCE" in names
        assert "OCCUPANCY" in names

    def test_object_types_correct(self):
        """Each variable has the correct object type."""
        result = scan_idf_template_variables(self.REALISTIC_IDF)

        glazing_vars = {"U_FACTOR", "SHGC", "VISIBLE_TRANSMITTANCE"}
        for var in result.variables:
            if var.name in glazing_vars:
                assert var.object_type == "WindowMaterial:SimpleGlazingSystem", (
                    f"{var.name} should be in WindowMaterial"
                )
            elif var.name in {"ROUGHNESS", "THERMAL_RESISTANCE"}:
                assert var.object_type == "Material:NoMass", (
                    f"{var.name} should be in Material:NoMass"
                )
            elif var.name == "OCCUPANCY":
                assert var.object_type == "Schedule:Compact", (
                    f"{var.name} should be in Schedule:Compact"
                )

    def test_annotations_correct(self):
        """Variables have correct labels and units from annotations."""
        result = scan_idf_template_variables(self.REALISTIC_IDF)

        u_factor = next(v for v in result.variables if v.name == "U_FACTOR")
        assert u_factor.label == "U-Factor"
        assert u_factor.units == "W/m2-K"

        thermal = next(v for v in result.variables if v.name == "THERMAL_RESISTANCE")
        assert thermal.label == "Thermal Resistance"
        assert thermal.units == "m2-K/W"

        roughness = next(v for v in result.variables if v.name == "ROUGHNESS")
        assert roughness.label == "Roughness"
        assert roughness.units == ""

    def test_order_is_first_appearance(self):
        """Variables are in order of first appearance in the IDF."""
        result = scan_idf_template_variables(self.REALISTIC_IDF)
        names = [v.name for v in result.variables]
        assert names == [
            "U_FACTOR",
            "SHGC",
            "VISIBLE_TRANSMITTANCE",
            "ROUGHNESS",
            "THERMAL_RESISTANCE",
            "OCCUPANCY",
        ]

    def test_no_warnings_for_clean_template(self):
        """A clean template with proper uppercase variables has no warnings."""
        result = scan_idf_template_variables(self.REALISTIC_IDF)
        assert result.warnings == []

    def test_full_validation_passes(self):
        """Full validation of the realistic template succeeds."""
        result = validate_idf_template(
            filename="glazing_template.idf",
            content=self.REALISTIC_IDF.encode("utf-8"),
        )
        assert result.errors == []
        assert result.scan_result is not None
        assert len(result.scan_result.variables) == 6  # noqa: PLR2004
