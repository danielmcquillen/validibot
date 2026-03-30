"""Restricted JSONPath environment for filter expression resolution.

Provides a security-hardened wrapper around ``python-jsonpath`` (RFC 9535)
for resolving paths that contain filter expressions, e.g.::

    ownedMember[?@.name=='RadiatorPanel'].ownedAttribute[?@.name=='emissivity'].defaultValue

Only a minimal subset of JSONPath is permitted.  The following features
are explicitly **blocked** before the library ever sees the input:

- Recursive descent (``..``) — could traverse an entire payload tree.
- Wildcards (``[*]``, ``.*``) — produce unbounded result sets.
- Slice notation (``[n:m]``) — can select large array ranges.
- Excess filter segments — capped to prevent O(n^k) chaining.

All built-in JSONPath functions (``match()``, ``search()``, ``length()``,
etc.) are removed from the environment so that filter expressions can
only perform comparisons, not regex evaluation or other operations.

This module is imported lazily — only when ``resolve_path()`` encounters
a path containing ``[?``.  It has zero impact on startup time or on the
majority of paths that use plain dot/bracket notation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from jsonpath import JSONPathEnvironment
from jsonpath import JSONPathNameError
from jsonpath import JSONPathSyntaxError
from jsonpath import JSONPathTypeError

from validibot.validations.constants import MAX_JSONPATH_FILTER_SEGMENTS

logger = logging.getLogger(__name__)

# ── Module-level singleton ────────────────────────────────────────────
#
# The environment is immutable after setup and thread-safe.  Creating it
# once avoids re-clearing function_extensions on every call.

_env = JSONPathEnvironment()
_env.function_extensions.clear()

# Pre-compiled pattern for detecting slice notation like [1:3] or [:5].
_SLICE_RE = re.compile(r"\[-?\d*:-?\d*\]")


def _validate_restrictions(path: str) -> None:
    """Enforce structural restrictions before the library parses the path.

    Raises ``ValueError`` for any pattern we don't allow.  These checks
    run on the raw path string — fast, no parsing, no allocation.
    """
    if ".." in path:
        msg = "Recursive descent ('..') is not permitted in data path expressions."
        raise ValueError(msg)

    if "[*]" in path or ".*" in path:
        msg = "Wildcards are not permitted in data path expressions."
        raise ValueError(msg)

    if _SLICE_RE.search(path):
        msg = "Slice notation is not permitted in data path expressions."
        raise ValueError(msg)

    filter_count = path.count("[?")
    if filter_count > MAX_JSONPATH_FILTER_SEGMENTS:
        msg = (
            f"Too many filter expressions ({filter_count} > "
            f"{MAX_JSONPATH_FILTER_SEGMENTS})."
        )
        raise ValueError(msg)


def resolve_jsonpath(data: Any, path: str) -> tuple[Any, bool]:
    """Resolve a JSONPath filter expression against *data*.

    Returns ``(value, True)`` for the first match, or ``(None, False)``
    when the expression is blocked, malformed, or matches nothing.

    This function is the *only* public entry point from
    ``resolve_path()`` — all security checks happen here.
    """
    try:
        _validate_restrictions(path)
    except ValueError:
        logger.warning(
            "JSONPath blocked by policy (path=%r)",
            path,
        )
        return None, False

    # Normalise: users write dot-notation paths without a leading '$'.
    jsonpath_expr = f"$.{path}" if not path.startswith("$") else path

    try:
        results = _env.findall(jsonpath_expr, data)
    except (
        JSONPathSyntaxError,
        JSONPathTypeError,
        JSONPathNameError,
    ):
        logger.warning(
            "JSONPath expression invalid (path=%r)",
            path,
        )
        return None, False

    if results:
        return results[0], True
    return None, False


def validate_jsonpath_syntax(path: str) -> None:
    """Check that *path* is a valid, policy-compliant filter expression.

    Used by form validation to reject bad expressions at authoring time,
    before any data is involved.

    Raises ``ValueError`` if the path is blocked or unparseable.
    """
    _validate_restrictions(path)

    jsonpath_expr = f"$.{path}" if not path.startswith("$") else path
    try:
        _env.compile(jsonpath_expr)
    except (
        JSONPathSyntaxError,
        JSONPathTypeError,
        JSONPathNameError,
    ) as exc:
        msg = f"Invalid JSONPath filter expression: {exc}"
        raise ValueError(msg) from exc
