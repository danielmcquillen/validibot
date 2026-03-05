"""IDF template scanning and validation utilities.

Provides functions for detecting ``$VARIABLE_NAME`` placeholders in
EnergyPlus IDF files, extracting contextual metadata (object type, field
annotation, label, units), and validating uploaded template files.

These utilities serve the **author-side** workflow: upload an IDF with
placeholders, detect variables, validate the file, and store metadata.
The complementary substitution function (``substitute_template_parameters``)
will be added in Phase 4.

**Why a custom parser?** EnergyPlus has ``eppy``, but it's destructive
(strips comments and sorts objects), pins an old beautifulsoup4 (2019),
carries Python 2 compatibility layers, and weighs 11 MB for ~80 lines
of needed logic.  We only need regex-based variable detection and
annotation extraction — not a full IDF model.

Phase: 2 (IDF Parsing Utility and Variable Detection) of the EnergyPlus
Parameterized Templates ADR.

References:
    - ADR: ``validibot-project/docs/adr/
      2026-03-04-energyplus-parameterized-templates.md``
    - EnergyPlus IDD Conventions:
      https://bigladdersoftware.com/epx/docs/22-2/interface-developer/idd-conventions.html
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import PurePosixPath

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

#: Detect ``$VARIABLE_NAME`` in case-sensitive mode: only uppercase letters,
#: digits, and underscores.  Must start with a letter.
VARIABLE_PATTERN_CASE_SENSITIVE = re.compile(r"\$([A-Z][A-Z0-9_]*)")

#: Detect ``$variable_name`` in case-insensitive mode: any letter case.
VARIABLE_PATTERN_CASE_INSENSITIVE = re.compile(r"\$([A-Za-z][A-Za-z0-9_]*)")

#: Used in case-sensitive mode to find *mixed-case* variables that the
#: primary pattern won't match — these produce warnings suggesting the
#: author rename or switch modes.
MIXED_CASE_VAR_PATTERN = re.compile(r"\$([A-Za-z][A-Za-z0-9_]*)")

#: Extract the ``!-`` field annotation from the comment portion of a line.
ANNOTATION_PATTERN = re.compile(r"!-\s*(.+)")

#: Extract units from an annotation, e.g. ``{W/m2-K}`` → ``W/m2-K``.
UNITS_PATTERN = re.compile(r"\{([^}]+)\}")

#: Bytes per kilobyte — used for human-readable file size formatting.
_KB = 1024

#: Warning threshold — files larger than this get a non-blocking warning.
TEMPLATE_MAX_SIZE_BYTES = 500 * _KB  # 500 KB


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TemplateVariableContext:
    """Context information for a template variable found in an IDF.

    Represents a single ``$VARIABLE_NAME`` occurrence with metadata
    extracted from the surrounding IDF context: the EnergyPlus object type
    it appears in, its field position within that object, and the ``!-``
    annotation (from which label and units are parsed).

    This is the intermediate representation between the raw IDF scan and
    the typed ``IDFTemplateVariable`` stored in ``EnergyPlusStepConfig``.
    """

    name: str
    """Variable name without the ``$`` prefix, e.g. ``'U_FACTOR'``."""

    object_type: str | None
    """IDF object type containing this variable, e.g.
    ``'WindowMaterial:SimpleGlazingSystem'``. ``None`` if the variable
    appears before any object header or after a malformed header."""

    field_position: int | None
    """0-based index of the value field within the object (object type
    header excluded).  ``None`` if position tracking failed."""

    field_annotation: str | None
    """Raw ``!-`` annotation text from the comment portion of the line,
    e.g. ``'U-Factor {W/m2-K}'``.  ``None`` if no annotation is present."""

    label: str
    """Human-readable label extracted from the annotation.  The portion of
    the annotation text outside the ``{units}`` block, stripped and
    cleaned.  Empty string if no annotation or no label text."""

    units: str
    """Units extracted from the annotation via the ``{...}`` pattern,
    e.g. ``'W/m2-K'``.  Empty string if no units detected."""

    line_number: int
    """1-based line number in the IDF where this variable first appears."""


@dataclass
class ScanResult:
    """Result of scanning an IDF template for ``$VARIABLE_NAME`` placeholders.

    Separates detected variables from any warnings (e.g., mixed-case
    variables that won't be detected in case-sensitive mode).
    """

    variables: list[TemplateVariableContext] = field(default_factory=list)
    """Detected variables, deduplicated by name, in order of first
    appearance in the IDF."""

    warnings: list[str] = field(default_factory=list)
    """Non-blocking warnings produced during scanning (e.g., mixed-case
    variable suggestions)."""


@dataclass
class ValidationResult:
    """Result of validating an IDF template upload.

    Contains blocking errors (upload rejected) and non-blocking warnings
    (upload succeeds with cautions).  Also carries the ``ScanResult``
    when the scan was reached (i.e., no blocking errors prevented it).
    """

    errors: list[str] = field(default_factory=list)
    """Blocking errors — any non-empty list means the upload is rejected."""

    warnings: list[str] = field(default_factory=list)
    """Non-blocking warnings — shown to the author after a successful
    upload to surface potential issues."""

    scan_result: ScanResult | None = None
    """The scan result, populated when the scanner ran successfully
    (no blocking errors prevented it)."""


# ---------------------------------------------------------------------------
# IDF template scanner
# ---------------------------------------------------------------------------


def _split_data_and_comment(line: str) -> tuple[str, str]:
    """Split an IDF line into data and comment portions at the first ``!``.

    Returns ``(data, comment)`` where *comment* includes the leading ``!``.
    If no ``!`` is present, *comment* is the empty string.
    """
    idx = line.find("!")
    if idx == -1:
        return line, ""
    return line[:idx], line[idx:]


def _extract_annotation(comment: str) -> str | None:
    """Extract the ``!-`` field annotation from a comment portion.

    Returns the annotation text (without the ``!-`` prefix), or ``None``
    if the comment is a regular comment (``!`` without ``-``).
    """
    m = ANNOTATION_PATTERN.search(comment)
    if m:
        return m.group(1).strip()
    return None


def _parse_label_and_units(annotation: str | None) -> tuple[str, str]:
    """Parse label and units from an annotation string.

    The units pattern ``{...}`` is extracted first, then the remaining
    text (with the ``{...}`` portion removed) becomes the label.

    Returns ``(label, units)`` — both may be empty strings.
    """
    if not annotation:
        return "", ""

    units_match = UNITS_PATTERN.search(annotation)
    if units_match:
        units = units_match.group(1).strip()
        # Label is the annotation text with the {units} removed and trimmed.
        label = UNITS_PATTERN.sub("", annotation).strip()
    else:
        units = ""
        label = annotation.strip()

    return label, units


def _detect_object_type(data: str) -> str | None:
    """Detect an IDF object type header from the data portion of a line.

    **Must only be called when we expect a new object header** — i.e.,
    after a semicolon terminated the previous object or at the start of
    the file.  This constraint is enforced by the caller
    (``scan_idf_template_variables``), not here.

    IDF object types are the first comma-delimited token on a header line.
    Some have colons (``WindowMaterial:SimpleGlazingSystem``), some don't
    (``Zone``, ``Building``, ``Version``, ``Timestep``).  Because this
    function is only called in the "looking for header" state, we don't
    need the colon heuristic — just check that the line has a comma
    (object headers always end with one) and the first token isn't a
    digit-starting value or a ``$VARIABLE`` placeholder.

    Returns the object type string, or ``None`` if not a header.
    """
    # Object headers always contain a comma after the type name.
    if "," not in data:
        return None

    first_token = data.split(",", 1)[0].strip()

    if not first_token:
        return None

    # Reject tokens that start with a digit (these are values, not types).
    if first_token[0].isdigit():
        return None

    # Reject tokens that look like variable placeholders.
    if first_token.startswith("$"):
        return None

    return first_token


def scan_idf_template_variables(
    idf_text: str,
    *,
    case_sensitive: bool = True,
) -> ScanResult:
    """Scan an IDF template for ``$VARIABLE_NAME`` placeholders.

    Extracts variable names from the **data portions** of each line (not
    from comments), along with contextual metadata: which EnergyPlus
    object type the variable lives in, its field position within that
    object, and the ``!-`` field annotation (label + units).

    Variables are returned in **order of first appearance**, not
    alphabetically.  Duplicates are deduplicated — only the first
    occurrence is returned.

    Args:
        idf_text: Complete IDF file content as a string.
        case_sensitive: If ``True`` (default), only ``$UPPERCASE_NAMES``
            matching ``[A-Z][A-Z0-9_]*`` are detected.  Mixed-case
            patterns like ``$u_factor`` are reported as warnings.
            If ``False``, any case is matched and normalized to uppercase.

    Returns:
        A ``ScanResult`` containing the list of detected variables and
        any warnings.
    """
    pattern = (
        VARIABLE_PATTERN_CASE_SENSITIVE
        if case_sensitive
        else VARIABLE_PATTERN_CASE_INSENSITIVE
    )

    variables: list[TemplateVariableContext] = []
    seen_names: set[str] = set()
    warnings: list[str] = []

    # State tracking for IDF structure.
    current_object_type: str | None = None
    current_field_position: int = 0
    is_header_line: bool = False

    for line_number, raw_line in enumerate(idf_text.splitlines(), start=1):
        stripped = raw_line.strip()

        # Skip empty lines.
        if not stripped:
            continue

        # Skip full-line comments (``! ...``).
        if stripped.startswith("!"):
            continue

        data, comment = _split_data_and_comment(raw_line)

        # ── Object type detection ─────────────────────────────────
        # Only try to detect a new object type after a semicolon
        # terminated the previous object (or at the start of the file).
        # This prevents values like ``Until: 17:00`` inside a
        # Schedule:Compact object from being mistaken for new object
        # headers.
        if current_object_type is None:
            detected_type = _detect_object_type(data)
            if detected_type is not None:
                current_object_type = detected_type
                current_field_position = 0
                is_header_line = True
            else:
                is_header_line = False
        else:
            is_header_line = False

        # ── Extract annotation from comment ───────────────────────
        annotation_text = _extract_annotation(comment)
        label, units = _parse_label_and_units(annotation_text)

        # ── Scan data portion for variables ───────────────────────
        for m in pattern.finditer(data):
            var_name = m.group(1)
            if not case_sensitive:
                var_name = var_name.upper()

            if var_name in seen_names:
                continue

            seen_names.add(var_name)

            # Compute field position: count commas before this match
            # in the data portion.  On header lines the first comma
            # terminates the object type token, so subtract 1.
            commas_before = data[: m.start()].count(",")
            field_pos = commas_before
            if is_header_line:
                field_pos = max(0, commas_before - 1)

            variables.append(
                TemplateVariableContext(
                    name=var_name,
                    object_type=current_object_type,
                    field_position=current_field_position + field_pos,
                    field_annotation=annotation_text,
                    label=label,
                    units=units,
                    line_number=line_number,
                )
            )

        # ── Advance field position for next line ──────────────────
        data_commas = data.count(",")
        if is_header_line:
            # Header line: first comma ends the object type, remaining
            # commas are value-field separators.
            current_field_position += max(0, data_commas - 1)
        else:
            current_field_position += data_commas

        # Semicolon terminates the object — reset for the next one.
        if ";" in data:
            current_object_type = None
            current_field_position = 0

    # ── Mixed-case warnings (case-sensitive mode only) ────────────
    if case_sensitive:
        _emit_mixed_case_warnings(idf_text, seen_names, warnings)

    return ScanResult(variables=variables, warnings=warnings)


def _emit_mixed_case_warnings(
    idf_text: str,
    detected_names: set[str],
    warnings: list[str],
) -> None:
    """Scan for mixed-case ``$variable`` patterns that case-sensitive mode misses.

    In case-sensitive mode, only ``$UPPERCASE`` patterns are detected.
    This second pass finds any ``$mixedCase`` or ``$lowercase`` patterns
    in data portions and emits warnings suggesting the author rename them
    or switch to case-insensitive mode.
    """
    warned_names: set[str] = set()

    for line_number, raw_line in enumerate(idf_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("!"):
            continue

        data, _ = _split_data_and_comment(raw_line)

        for m in MIXED_CASE_VAR_PATTERN.finditer(data):
            var_name = m.group(1)
            # Skip variables that the primary pattern already detected.
            if var_name in detected_names:
                continue
            # Skip variables we already warned about.
            if var_name in warned_names:
                continue
            warned_names.add(var_name)

            upper_name = var_name.upper()
            if upper_name in detected_names:
                # The uppercase version was detected — warn about the alias.
                warnings.append(
                    f"Line {line_number}: '${var_name}' looks like a "
                    f"template variable but uses non-uppercase characters. "
                    f"In case-sensitive mode, only $UPPERCASE_NAMES are "
                    f"detected. The uppercase form ${upper_name} was "
                    f"already detected. Rename to '${upper_name}' or "
                    f"set case_sensitive=False."
                )
            else:
                warnings.append(
                    f"Line {line_number}: '${var_name}' looks like a "
                    f"template variable but uses non-uppercase characters. "
                    f"In case-sensitive mode, only $UPPERCASE_NAMES are "
                    f"detected. Rename to '${upper_name}' or set "
                    f"case_sensitive=False."
                )


# ---------------------------------------------------------------------------
# IDF template validation
# ---------------------------------------------------------------------------


def validate_idf_template(
    *,
    filename: str,
    content: bytes,
    case_sensitive: bool = True,
) -> ValidationResult:
    """Validate an IDF template file uploaded by a workflow author.

    Runs a series of checks — from cheap (file extension) to expensive
    (variable scanning).  The first **blocking error** stops the chain
    and rejects the upload.  Non-blocking **warnings** are accumulated
    and returned alongside the scan result so the author sees them on
    the Template Variables card.

    Args:
        filename: Original filename of the uploaded file.
        content: Raw file content as bytes.
        case_sensitive: Passed through to ``scan_idf_template_variables``.

    Returns:
        A ``ValidationResult`` with errors, warnings, and (if the scan
        succeeded) the ``ScanResult``.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ── 1. File extension check ───────────────────────────────────
    ext = PurePosixPath(filename).suffix
    if ext.lower() != ".idf":
        errors.append(
            f"Template files must be IDF format (.idf). epJSON templates "
            f"are not supported — see the ADR scope note. You uploaded "
            f"'{filename}'."
        )
        return ValidationResult(errors=errors, warnings=warnings)

    # ── 2. Text encoding check ────────────────────────────────────
    if b"\x00" in content:
        errors.append(
            "The uploaded file does not appear to be a text file. "
            "IDF templates must be plain text."
        )
        return ValidationResult(errors=errors, warnings=warnings)

    text: str | None = None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        with contextlib.suppress(UnicodeDecodeError):
            text = content.decode("latin-1")

    if text is None:
        errors.append(
            "The uploaded file does not appear to be a text file. "
            "IDF templates must be plain text."
        )
        return ValidationResult(errors=errors, warnings=warnings)

    # ── 3. Not empty ──────────────────────────────────────────────
    has_data_line = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("!"):
            has_data_line = True
            break

    if not has_data_line:
        errors.append(
            "The uploaded IDF file is empty or contains only comments. "
            "A template must contain at least one EnergyPlus object with "
            "$VARIABLE_NAME placeholders."
        )
        return ValidationResult(errors=errors, warnings=warnings)

    # ── 4. Basic IDF structure checks ─────────────────────────────

    # 4a. First non-whitespace character: reject JSON disguised as IDF.
    first_char = text.lstrip()[:1]
    if first_char in ("{", "["):
        errors.append(
            "This file appears to be JSON, not IDF format. If this is an "
            "epJSON file, rename it to .epjson. Parameterized templates "
            "only support IDF format."
        )
        return ValidationResult(errors=errors, warnings=warnings)

    # 4b. At least one semicolon in data lines.
    has_semicolon = False
    has_colon_token = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        data, _ = _split_data_and_comment(line)
        if ";" in data:
            has_semicolon = True
        # Check for object-type tokens (contain a colon, don't start
        # with a digit).
        for token in data.split(","):
            tok = token.strip()
            if tok and ":" in tok and not tok[0].isdigit():
                has_colon_token = True

    if not has_semicolon:
        errors.append(
            "This file does not appear to be a valid IDF — no "
            "object-terminating semicolons found. IDF objects must end "
            "with a semicolon (;). Check that you uploaded the correct "
            "file."
        )
        return ValidationResult(errors=errors, warnings=warnings)

    if not has_colon_token:
        errors.append(
            "This file does not appear to be a valid IDF — no EnergyPlus "
            "object types found. IDF objects start with a type name "
            "containing a colon (e.g., 'Zone:', 'Material:NoMass'). "
            "Check that you uploaded the correct file."
        )
        return ValidationResult(errors=errors, warnings=warnings)

    # ── 5. Variable scan ──────────────────────────────────────────
    scan_result = scan_idf_template_variables(text, case_sensitive=case_sensitive)

    if not scan_result.variables:
        errors.append(
            "No template variables found. A parameterized template must "
            "contain at least one $VARIABLE_NAME placeholder in a field "
            "value (not in a comment). Variable names must start with a "
            "letter and contain only uppercase letters, digits, and "
            "underscores (e.g., $U_FACTOR, $SHGC). If you intended to "
            "use this IDF without parameters, use a standard "
            "(non-template) EnergyPlus step instead."
        )
        return ValidationResult(errors=errors, warnings=warnings)

    # ── 6. Non-blocking warnings ──────────────────────────────────

    # 6a. Carry forward mixed-case warnings from scanner.
    warnings.extend(scan_result.warnings)

    # 6b. Duplicate variable appearances (same name on multiple lines).
    _emit_duplicate_warnings(text, case_sensitive=case_sensitive, warnings=warnings)

    # 6c. Invalid dollar patterns.
    _emit_invalid_dollar_warnings(text, warnings)

    # 6d. Large file warning.
    if len(content) > TEMPLATE_MAX_SIZE_BYTES:
        size_kb = len(content) / _KB
        size_str = f"{size_kb / _KB:.1f} MB" if size_kb >= _KB else f"{size_kb:.0f} KB"
        warnings.append(
            f"This template is {size_str}. Large templates increase "
            f"substitution time and storage costs. Consider whether all "
            f"objects need to be in the template, or if some can be "
            f"included via EnergyPlus's native ##include mechanism."
        )

    return ValidationResult(
        errors=[],
        warnings=warnings,
        scan_result=scan_result,
    )


def _emit_duplicate_warnings(
    idf_text: str,
    *,
    case_sensitive: bool,
    warnings: list[str],
) -> None:
    """Emit informational warnings for variables that appear on multiple lines."""
    pattern = (
        VARIABLE_PATTERN_CASE_SENSITIVE
        if case_sensitive
        else VARIABLE_PATTERN_CASE_INSENSITIVE
    )

    # Map variable name → list of line numbers where it appears.
    occurrences: dict[str, list[int]] = {}

    for line_number, raw_line in enumerate(idf_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        data, _ = _split_data_and_comment(raw_line)
        for m in pattern.finditer(data):
            var_name = m.group(1)
            if not case_sensitive:
                var_name = var_name.upper()
            occurrences.setdefault(var_name, []).append(line_number)

    for var_name, lines in occurrences.items():
        if len(lines) > 1:
            line_list = ", ".join(str(ln) for ln in lines)
            warnings.append(
                f"${var_name} appears on lines {line_list}. All "
                f"occurrences will be replaced with the same value "
                f"during substitution."
            )


def _emit_invalid_dollar_warnings(
    idf_text: str,
    warnings: list[str],
) -> None:
    """Emit warnings for ``$`` signs not followed by a valid variable pattern.

    Catches common mistakes like ``$3_ZONES`` (starts with digit),
    ``$my-var`` (contains hyphen), or bare ``$`` characters.
    """
    # Pattern to find $ followed by something that is NOT a valid variable.
    # We look for $ followed by characters that could be an attempted name.
    dollar_pattern = re.compile(r"\$(\S*)")
    valid_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

    warned: set[str] = set()

    for line_number, raw_line in enumerate(idf_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        data, _ = _split_data_and_comment(raw_line)
        for m in dollar_pattern.finditer(data):
            following = m.group(1)
            # If it's a valid variable name, skip (the scanner handles it).
            if following and valid_pattern.match(following):
                continue
            # It's invalid — emit a warning.
            if following and following not in warned:
                warned.add(following)
                # Suggest a fix if it starts with a digit.
                if following[0].isdigit():
                    warnings.append(
                        f"Line {line_number}: Found '$' followed by "
                        f"'{following}' — this is not a valid variable "
                        f"name (must start with a letter, not a digit)."
                    )
                else:
                    warnings.append(
                        f"Line {line_number}: Found '$' followed by "
                        f"'{following}' — this is not a valid variable "
                        f"name (only letters, digits, and underscores "
                        f"are allowed)."
                    )
            elif not following and "$" not in warned:
                warned.add("$")
                warnings.append(
                    f"Line {line_number}: Found a bare '$' character "
                    f"with no variable name following it."
                )
