"""Restricted JSONPath environment for filter expression resolution.

Provides a security-hardened wrapper around ``python-jsonpath`` (RFC 9535)
for resolving paths that contain filter expressions, e.g.::

    ownedMember[?@.name=='RadiatorPanel'].ownedAttribute[?@.name=='emissivity'].defaultValue

Only a minimal subset of JSONPath is permitted.  The following features
are explicitly **blocked** before the library ever sees the input:

- Recursive descent (``..``) — could traverse an entire payload tree.
- Regex-match operator (``=~``) — a parser-level operator that survives
  clearing ``function_extensions`` and exposes a catastrophic-backtracking
  ReDoS surface via untimed ``re.fullmatch`` over every submission cell.
- Wildcards (``[*]``, ``.*``) — produce unbounded result sets.
- Slice notation (``[n:m]``) — can select large array ranges.
- Excess filter segments — capped to prevent O(n^k) chaining.

All built-in JSONPath functions (``match()``, ``search()``, ``length()``,
etc.) are removed from the environment so that filter expressions can
only perform comparisons, not regex evaluation or other operations.  The
``=~`` operator is *not* a function, so it is blocked separately by the
string-level check in ``_validate_restrictions``.

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

from validibot.validations._bounded_eval import ExpressionEvaluationTimeoutError
from validibot.validations._bounded_eval import run_with_timeout
from validibot.validations.constants import MAX_JSONPATH_FILTER_SEGMENTS

logger = logging.getLogger(__name__)

# ── Defense-in-depth wall-clock timeout ───────────────────────────────
#
# Even with the ``=~`` regex operator rejected at validation time (see
# ``_validate_restrictions``), a pathological filter expression evaluated
# over a very large submission (up to ~1M cells) could still consume an
# unbounded amount of CPU.  We cap the actual resolution call with a hard
# wall-clock timeout so a single path can never hang the worker process.
# This is a safety net, not the primary control — the ``=~`` rejection is.
JSONPATH_RESOLVE_TIMEOUT_SECONDS = 2.0

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

    # ── Regex-match operator (ReDoS guard) ────────────────────────────
    #
    # ``python-jsonpath`` parses the ``=~`` regex-match operator at the
    # grammar level, so clearing ``function_extensions`` (which removes
    # ``match()`` / ``search()``) does NOT disable it.  An author could
    # smuggle in a catastrophically-backtracking pattern such as
    # ``items[?@.name =~ /(a+)+$/]``; the library would then run
    # ``re.fullmatch`` (no timeout) over every cell of the submission,
    # hanging the worker.  Validibot's documented JSONPath subset is
    # comparison-only, so we reject ``=~`` outright before parsing.
    if "=~" in path:
        msg = (
            "Regex-match ('=~') is not permitted in data path expressions; "
            "only comparison operators (==, !=, <, <=, >, >=) are allowed."
        )
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

    # Run the actual resolution under a hard wall-clock timeout. The ``=~``
    # rejection above already removes the known ReDoS vector; bounding
    # ``findall`` with ``JSONPATH_RESOLVE_TIMEOUT_SECONDS`` ensures that *any*
    # future pathological expression — over an unbounded submission — can never
    # block this code path indefinitely.
    #
    # Evaluation runs on the shared, process-wide bounded pool (``_bounded_eval``)
    # rather than a fresh per-call executor: that caps the total number of
    # resolution threads the process can spawn (so repeated slow expressions
    # cannot accumulate unbounded threads) and avoids per-call pool churn. A
    # CPython thread cannot be forcibly killed, so a runaway ``findall`` keeps
    # running after the timeout fires; the helper deliberately does NOT wait on
    # it (waiting would re-block the caller), and the orphaned worker drains on
    # a pool thread in the background. With ``=~`` already rejected, reaching
    # this timeout requires a genuinely pathological-but-bounded expression.
    try:
        results = run_with_timeout(
            lambda: _env.findall(jsonpath_expr, data),
            timeout_s=JSONPATH_RESOLVE_TIMEOUT_SECONDS,
        )
    except ExpressionEvaluationTimeoutError:
        logger.warning(
            "JSONPath resolution timed out after %.1fs (path=%r)",
            JSONPATH_RESOLVE_TIMEOUT_SECONDS,
            path,
        )
        return None, False
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
