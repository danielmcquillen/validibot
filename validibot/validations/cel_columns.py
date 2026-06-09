"""Lexical (text-level) static analysis of CEL expressions.

Two pieces of the system read a CEL expression's *text* without fully parsing
it, and they must agree, so the scans live here and both call them:

1. **Tabular column references** (``referenced_tabular_columns``) — which
   ``row.<column>`` or ``col.<column>`` an assertion uses. The assertion form
   (``validations/forms.py``) and the workflow importer (the Tabular step
   serializer) both call it; if they disagreed, a row assertion that saved
   cleanly in the editor could be rejected on import.
2. **Macro-bound loop variables** (``bound_macro_variables``) — the temporary
   variable a comprehension macro (``all``/``exists``/``map``/``filter``/
   ``exists_one``) introduces, e.g. ``ns`` in ``items.all(ns, ns in allowed)``.
   The identifier checks in ``forms.py`` use it so a loop variable of any length
   isn't mistaken for an un-namespaced data reference.

Both scans handle the same subtlety: a token appearing *inside a CEL string
literal* (e.g. ``"row.notAColumn"`` or ``".all(x,"``) is not real syntax, so
string literals are stripped/masked before scanning.
"""

from __future__ import annotations

import re

# A sentinel that can't appear in CEL source, used to mask out string literals.
_NUL = "\x00"
# ``row.<identifier>`` not preceded by a word char or dot (so ``arrow.x`` and
# ``foo.row.x`` don't match). Scanned on the *masked* expression (literals
# replaced by sentinels), so a ``row.x`` inside a string literal can't match.
_TABULAR_DOT_RE = re.compile(
    r"(?:^|[^\w.])(?P<namespace>row|col)\.([A-Za-z_][A-Za-z0-9_]*)",
)
# A bracket access whose key is a (masked) string literal: ``row[<sentinel>]``.
# Because only a *real* bracket access leaves the literal sentinel directly
# inside ``row[...]``, a ``row["x"]`` that itself sits inside an outer literal
# (the whole thing is one masked literal) never matches.
_TABULAR_BRACKET_RE = re.compile(
    rf"(?P<namespace>row|col)\s*\[\s*{_NUL}(\d+){_NUL}\s*\]",
)
_COLUMN_DOT_METRIC_RE = re.compile(
    r"(?:^|[^\w.])col\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)",
)
_COLUMN_BRACKET_METRIC_RE = re.compile(
    rf"col\s*\[\s*{_NUL}(\d+){_NUL}\s*\]\.([A-Za-z_][A-Za-z0-9_]*)",
)


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


def referenced_tabular_columns(expression: str, namespace: str) -> set[str]:
    """Return columns referenced through the requested tabular namespace.

    ``namespace`` must be ``row`` or ``col``. The scan is literal-aware for both
    dot and bracket access, so text such as ``"col.notAColumn"`` is not mistaken
    for a real reference.
    """
    if namespace not in {"row", "col"}:
        msg = "Tabular CEL namespace must be 'row' or 'col'."
        raise ValueError(msg)
    masked, literals = _mask_string_literals(expression)
    columns = {
        match.group(2)
        for match in _TABULAR_DOT_RE.finditer(masked)
        if match.group("namespace") == namespace
    }
    for match in _TABULAR_BRACKET_RE.finditer(masked):
        if match.group("namespace") != namespace:
            continue
        index = int(match.group(2))
        if 0 <= index < len(literals):
            columns.add(literals[index])
    return columns


def referenced_row_columns(expression: str) -> set[str]:
    """Return columns referenced through ``row.*``."""
    return referenced_tabular_columns(expression, "row")


def referenced_column_aggregates(expression: str) -> set[str]:
    """Return columns referenced through the V2 ``col.*`` namespace."""
    return referenced_tabular_columns(expression, "col")


def referenced_column_metrics(expression: str) -> set[tuple[str, str]]:
    """Return ``(column, metric)`` pairs selected from ``col.*``."""
    masked, literals = _mask_string_literals(expression)
    metrics = {
        (match.group(1), match.group(2))
        for match in _COLUMN_DOT_METRIC_RE.finditer(masked)
    }
    for match in _COLUMN_BRACKET_METRIC_RE.finditer(masked):
        index = int(match.group(1))
        if 0 <= index < len(literals):
            metrics.add((literals[index], match.group(2)))
    return metrics


# A comprehension macro binds its first argument as a loop variable:
# ``<receiver>.all(VAR, <body>)`` and likewise for exists/exists_one/map/filter.
# ``exists_one`` is listed before ``exists`` only for readability — the trailing
# ``\s*\(`` makes the alternation unambiguous either way. ``has`` is excluded: it
# takes a single field-selection argument and binds nothing.
_MACRO_BOUND_VAR_RE = re.compile(
    r"\.(?:all|exists_one|exists|map|filter)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,",
)


def bound_macro_variables(expression: str) -> set[str]:
    """Return the loop-variable names a CEL comprehension macro introduces.

    CEL's ``all``/``exists``/``exists_one``/``map``/``filter`` macros bind their
    first argument as a temporary variable scoped to the macro body — e.g. ``ns``
    in ``items.all(ns, ns in allowed)``. That name is *defined by the expression*,
    not a free data reference, so the identifier checks that insist every name be
    namespace-prefixed (``i.`` / ``p.`` / ``s.``) must exempt it — including a
    multi-letter name, which the old single-letter-only shortcut wrongly rejected.

    Scanned on the literal-stripped expression so a macro-looking token inside a
    string (``"items.all(x,"``) doesn't count.
    """
    stripped = strip_cel_string_literals(expression)
    return {match.group(1) for match in _MACRO_BOUND_VAR_RE.finditer(stripped)}
