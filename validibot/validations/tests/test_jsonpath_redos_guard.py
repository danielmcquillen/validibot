"""Regression tests for the JSONPath ReDoS guard (``=~`` rejection).

WHY THIS SUITE EXISTS
---------------------
``validibot.validations.services._jsonpath_env`` wraps ``python-jsonpath``
in a deliberately restricted environment.  Clearing
``function_extensions`` removes the ``match()`` / ``search()`` *functions*,
but the ``=~`` regex-match *operator* is parsed at the grammar level and
therefore survives that clearing.  Left unguarded, an author could submit a
filter such as ``items[?@.name =~ /(a+)+$/]`` whose catastrophic
backtracking would run an untimed ``re.fullmatch`` over every cell of a
submission (up to ~1M cells) and hang the Celery worker — a HIGH-severity
denial-of-service (ReDoS).

These tests pin the primary control: ``_validate_restrictions`` must reject
any path containing ``=~`` before the library ever parses it, while leaving
the documented comparison-only subset working.  They guard against a future
refactor silently re-opening the regex operator.
"""

from __future__ import annotations

import pytest

from validibot.validations.services._jsonpath_env import _validate_restrictions
from validibot.validations.services._jsonpath_env import resolve_jsonpath


# ── Primary control: ``=~`` is rejected at validation time ────────────
#
# This is the load-bearing assertion for the fix.  The regex-match
# operator must never reach the ``python-jsonpath`` parser, because once
# parsed it executes untimed regex evaluation that enables ReDoS.
def test_regex_match_operator_is_rejected() -> None:
    """A path using the ``=~`` regex operator must raise ``ValueError``.

    WHY: ``=~`` is a parser-level operator that ``function_extensions.clear()``
    does not remove.  If ``_validate_restrictions`` ever stops rejecting it,
    a malicious filter like ``items[?@.name =~ /(a+)+$/]`` would compile and
    run catastrophic-backtracking regex over the whole submission, hanging
    the worker.  The error message must also name ``=~`` so authors can fix
    their expression.
    """
    redos_path = "items[?@.name =~ /(a+)+$/]"

    with pytest.raises(ValueError, match="=~") as exc_info:
        _validate_restrictions(redos_path)

    # The message should steer authors toward the allowed comparison subset.
    assert "comparison" in str(exc_info.value).lower()

    # And the public entry point must fail closed: blocked -> (None, False),
    # never raising and never invoking the regex engine on submitter data.
    assert resolve_jsonpath({"items": [{"name": "x"}]}, redos_path) == (None, False)


# ── Negative control: the allowed comparison subset still works ───────
#
# The guard must be narrow.  Rejecting ``=~`` should not accidentally
# break the documented equality-comparison filters that the feature
# exists to support.
def test_comparison_filter_is_still_permitted() -> None:
    """A comparison-only filter must pass validation unchanged.

    WHY: The fix narrows the allowed grammar, so we assert it did not
    over-reach.  ``==`` (and the other comparison operators) are the whole
    point of the restricted JSONPath subset; if the ``=~`` check were
    written too loosely it could reject legitimate equality filters too.
    """
    # Must not raise — this is the canonical supported usage.
    _validate_restrictions("ownedAttribute[?@.name=='emissivity'].defaultValue")
