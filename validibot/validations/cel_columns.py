"""Static analysis of CEL row assertions: which ``row.<column>`` they reference.

Two code paths need this answer and **must agree**:

- the assertion authoring form (``validations/forms.py``), to reject a row
  assertion that references a column the tabular schema doesn't declare; and
- the workflow importer (the Tabular Validator's step serializer), to re-apply
  that same check on a re-imported ruleset.

If they disagreed, a row assertion that saved cleanly in the editor could be
rejected on import (or vice versa). Keeping the scan here — used by both — is
what guarantees they can't drift. The subtlety this exists to handle: a column
name appearing *inside a CEL string literal* (e.g. ``"row.notAColumn"``) is not a
real reference, so literals are stripped before the dot-access scan.
"""

from __future__ import annotations

import re

# A sentinel that can't appear in CEL source, used to mask out string literals.
_NUL = "\x00"
# ``row.<identifier>`` not preceded by a word char or dot (so ``arrow.x`` and
# ``foo.row.x`` don't match). Scanned on the *masked* expression (literals
# replaced by sentinels), so a ``row.x`` inside a string literal can't match.
_ROW_DOT_RE = re.compile(r"(?:^|[^\w.])row\.([A-Za-z_][A-Za-z0-9_]*)")
# A bracket access whose key is a (masked) string literal: ``row[<sentinel>]``.
# Because only a *real* bracket access leaves the literal sentinel directly
# inside ``row[...]``, a ``row["x"]`` that itself sits inside an outer literal
# (the whole thing is one masked literal) never matches.
_ROW_BRACKET_RE = re.compile(rf"row\s*\[\s*{_NUL}(\d+){_NUL}\s*\]")


def _mask_string_literals(expression: str) -> tuple[str, list[str]]:
    """Replace each top-level CEL string literal with a ``\\x00<idx>\\x00`` sentinel.

    Returns ``(masked, literals)`` where ``literals[idx]`` is the content of the
    i-th literal (between its quotes, with backslash escapes resolved). Masking —
    rather than simply deleting literals — is what lets bracket access be scanned
    correctly: a *real* ``row["col"]`` reference's quoted column name becomes a
    sentinel sitting inside ``row[...]`` (recoverable), while a ``row["col"]`` that
    is itself wholly inside an outer literal collapses into a single sentinel that
    no ``row[...]`` pattern matches.
    """
    masked: list[str] = []
    literals: list[str] = []
    quote: str | None = None
    escaped = False
    content: list[str] = []

    for char in expression:
        if quote is None:
            if char in {"'", '"'}:
                quote = char
                escaped = False
                content = []
            else:
                masked.append(char)
            continue

        if escaped:
            escaped = False
            content.append(char)
        elif char == "\\":
            escaped = True
        elif char == quote:
            quote = None
            masked.append(f"{_NUL}{len(literals)}{_NUL}")
            literals.append("".join(content))
        else:
            content.append(char)

    return "".join(masked), literals


def strip_cel_string_literals(expression: str) -> str:
    """Remove CEL string literals from *expression*.

    A preprocessing step so identifier-shaped tokens inside string literals
    (``"row.foo"``) aren't mistaken for bare references. Handles single and double
    quotes and backslash escapes. The caller bounds input length.
    """
    output: list[str] = []
    quote: str | None = None
    escaped = False

    for char in expression:
        if quote is None:
            if char in {"'", '"'}:
                quote = char
                escaped = False
            else:
                output.append(char)
            continue

        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            quote = None

    return "".join(output)


def referenced_row_columns(expression: str) -> set[str]:
    """Return the column names a row CEL expression references via ``row.*``.

    Literal-aware for *both* spellings: a column-name-shaped token inside a CEL
    string literal — whether ``"row.x"`` (dot) or ``'row["x"]'`` (bracket) — is not
    a reference. A genuine ``row.lat`` / ``row["dwc:eventDate"]`` still is.
    """
    masked, literals = _mask_string_literals(expression)
    columns = {match.group(1) for match in _ROW_DOT_RE.finditer(masked)}
    for match in _ROW_BRACKET_RE.finditer(masked):
        index = int(match.group(1))
        if 0 <= index < len(literals):
            columns.add(literals[index])
    return columns
