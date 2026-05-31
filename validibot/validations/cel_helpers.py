"""
Executable CEL helper functions — the *runtime* binding for Validibot's
custom CEL helpers.

A CEL helper needs three separate registrations, and this module is the
third — the one that actually makes the helper callable when an
expression is evaluated:

1. ``DEFAULT_HELPERS`` in :mod:`validibot.validations.cel` — *documentation*
   metadata that drives the authoring UI's help tooltip. Authoring-time.
2. The identifier allowlist in :mod:`validibot.validations.forms`
   (``custom_helpers`` in ``RulesetAssertionForm._validate_cel_identifiers``)
   — *authoring-time* validation so the form accepts the helper name on
   save. Kept in sync with this module via :data:`V1_CEL_HELPER_NAMES`.
3. **This module** — the Python implementations celpy invokes, bound onto
   a program via ``celpy.Environment.program(ast, functions=...)``.

Before this module existed, helpers were registered only in (1) and (2):
an expression like ``is_iso8601(row.eventDate)`` would *save* cleanly and
then raise an unknown-function error at evaluation time. Closing that gap
is the platform prerequisite the Tabular Validator forces — see
ADR-2026-05-26 (Tabular Validator), "CEL helpers added in V1".

celpy passes CEL-native values (``celpy.celtypes`` instances) to these
callables and expects a CEL-native value back — or ``None`` for CEL
``null``. The implementations therefore accept ``Any`` (the incoming
celtype, e.g. ``StringType`` which subclasses ``str``) and return an
explicit celtype.

Determinism note: every helper here is locale-independent and
side-effect-free, and ``now()`` is pinned to a supplied run clock rather
than reading the wall clock. This is what lets a downstream credential
attest over reproducible findings (ADR-2026-05-26, "Determinism and
signing").
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC
from datetime import date
from datetime import datetime
from typing import Any

from celpy import celtypes as ct

# A celpy-compatible function: receives celtypes, returns a celtype (or
# ``None`` for CEL ``null``). celpy exposes a richer ``CELFunction`` union
# internally; ``Callable[..., Any]`` is the practical public shape.
CelFunction = Callable[..., Any]

# CEL boolean singletons. ``BoolType(<literal>)`` is a constructor call with
# a positional bool — unavoidable for a celtype wrapper — so the FBT003
# acknowledgements live here once rather than at every predicate return.
_CEL_TRUE = ct.BoolType(True)  # noqa: FBT003 - celtype wrapper needs the literal
_CEL_FALSE = ct.BoolType(False)  # noqa: FBT003 - celtype wrapper needs the literal


def _parse_iso8601(text: str) -> datetime | None:
    """Parse an ISO 8601 date or datetime string into a tz-aware datetime.

    Locale-independent and fixed-format by design (the determinism
    contract in ADR-2026-05-26): only ISO 8601 is accepted, parsing never
    consults ``LC_TIME``/``LC_NUMERIC``, and a naive input is interpreted
    as UTC so two operators on two machines resolve the same instant.

    Returns ``None`` when *text* is not valid ISO 8601 — callers turn that
    into CEL ``null`` (a failing assertion), never a silent pass.
    """
    candidate = text.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        # ``datetime.fromisoformat`` accepts "YYYY-MM-DD" on 3.11+, but
        # be explicit about the date-only fallback for clarity.
        try:
            day = date.fromisoformat(candidate)
        except ValueError:
            return None
        parsed = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if parsed.tzinfo is None:
        # Naive → assume UTC for determinism (see docstring).
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def cel_is_iso8601(value: Any) -> ct.BoolType:
    """``is_iso8601(value) -> bool``: true iff *value* is an ISO 8601 string.

    Non-string and null inputs return ``false`` — they are not valid ISO
    8601 strings — so a missing or wrongly-typed date cell fails a format
    check rather than crashing the pass or silently passing.
    """
    if not isinstance(value, str):
        return _CEL_FALSE
    return ct.BoolType(_parse_iso8601(value) is not None)


def cel_parse_date(value: Any) -> ct.TimestampType | None:
    """``parse_date(value) -> timestamp | null``: parse an ISO 8601 string.

    Intended for columns left as ``type=string``; a ``type=date`` column
    already exposes a ``timestamp``, so ``parse_date`` is unnecessary (and
    wrong) there. Returns CEL ``null`` on a non-string or unparseable
    value — per the determinism contract a downstream comparison against
    null is a failure, never a silent pass.
    """
    if not isinstance(value, str):
        return None
    parsed = _parse_iso8601(value)
    if parsed is None:
        return None
    return ct.TimestampType(parsed)


def cel_is_finite(value: Any) -> ct.BoolType:
    """``is_finite(value) -> bool``: true iff *value* is a finite number.

    NaN, +Inf, -Inf, null, and non-numbers all return ``false``. A boolean
    returns ``false`` too: although celpy's ``BoolType`` (and Python
    ``bool``) subclass ``int``, a boolean is not a number for this
    predicate, so we exclude it explicitly before the numeric check.
    """
    if isinstance(value, (bool, ct.BoolType)):
        return _CEL_FALSE
    if isinstance(value, (int, float)):
        # ``math.isfinite`` accepts int and float directly; passing the
        # int avoids an OverflowError that ``float(huge_int)`` would raise.
        return ct.BoolType(math.isfinite(value))
    return _CEL_FALSE


def cel_is_int(value: Any) -> ct.BoolType:
    """``is_int(value) -> bool``: true iff *value* is an integral number.

    ``2`` and ``2.0`` are integral (true); ``2.5`` is not (false). A
    boolean returns ``false`` (it is not a number for this predicate),
    as do NaN/Inf, null, and non-numbers — so a guard like
    ``is_int(row.count)`` fails cleanly on a bad cell rather than raising.
    """
    if isinstance(value, (bool, ct.BoolType)):
        return _CEL_FALSE
    if isinstance(value, int):  # IntType subclasses int (bool excluded above)
        return _CEL_TRUE
    if isinstance(value, float):
        return ct.BoolType(math.isfinite(value) and float(value).is_integer())
    return _CEL_FALSE


def cel_abs(value: Any) -> ct.IntType | ct.DoubleType | None:
    """``abs(value) -> number``: absolute value, preserving int vs. double.

    Returns ``null`` for a boolean, null, or non-number so the result of a
    bad input is a failing comparison, never a raised error inside a tight
    loop.
    """
    if isinstance(value, (bool, ct.BoolType)):
        return None
    if isinstance(value, int):  # IntType
        return ct.IntType(abs(value))
    if isinstance(value, float):  # DoubleType
        return ct.DoubleType(abs(value))
    return None


def cel_round(value: Any, digits: Any = 0) -> ct.DoubleType | None:
    """``round(value, digits=0) -> number``: round to *digits* decimals.

    Uses Python's round-half-to-even ("banker's rounding"), which is fully
    deterministic — the property that matters for a reproducible
    attestation, even if it occasionally surprises (``round(0.5) == 0``).
    Returns a double; ``digits`` is optional and defaults to 0. A boolean,
    null, or non-number value returns ``null``.
    """
    if isinstance(value, (bool, ct.BoolType)):
        return None
    if isinstance(value, (int, float)):
        return ct.DoubleType(round(float(value), int(digits)))
    return None


def _numeric_list(values: Any) -> list[float] | None:
    """Coerce a CEL list into a list of floats for an aggregate helper.

    Nulls are *ignored* (skipped), matching the documented "ignores nulls"
    contract. The result is ``None`` — which the aggregates turn into CEL
    ``null`` — when the input is not a list, or when any non-null element
    is not a number (a boolean counts as non-numeric here). Returning
    ``null`` for malformed input makes a downstream comparison fail rather
    than silently computing over garbage.
    """
    if not isinstance(values, list):
        return None
    out: list[float] = []
    for item in values:
        if item is None:
            continue
        if isinstance(item, (bool, ct.BoolType)):
            return None
        if isinstance(item, (int, float)):
            out.append(float(item))
        else:
            return None
    return out


def cel_mean(values: Any) -> ct.DoubleType | None:
    """``mean(values) -> number``: arithmetic mean of the numeric items.

    Ignores nulls. Returns CEL ``null`` for an empty/all-null list or
    malformed input (the mean of nothing is undefined). The result is a
    double — compare it against a double literal (``mean(xs) > 1.0``);
    celpy rejects ``double == int`` equality.
    """
    nums = _numeric_list(values)
    if not nums:
        return None
    return ct.DoubleType(sum(nums) / len(nums))


def cel_sum(values: Any) -> ct.DoubleType | None:
    """``sum(values) -> number``: sum of the numeric items (nulls ignored).

    An empty list sums to ``0.0`` (the identity), but malformed input
    (non-list, or a non-numeric element) returns ``null``. Returns a double.
    """
    nums = _numeric_list(values)
    if nums is None:
        return None
    return ct.DoubleType(sum(nums))


def cel_min(values: Any) -> ct.DoubleType | None:
    """``min(values) -> number``: smallest numeric item (nulls ignored).

    Returns CEL ``null`` for an empty/all-null list or malformed input.
    The function form complements celpy's ``[...].min()`` method; this one
    matches the documented ``min(values)`` signature and returns a double.
    """
    nums = _numeric_list(values)
    if not nums:
        return None
    return ct.DoubleType(min(nums))


def cel_max(values: Any) -> ct.DoubleType | None:
    """``max(values) -> number``: largest numeric item (nulls ignored).

    Returns CEL ``null`` for an empty/all-null list or malformed input.
    Returns a double.
    """
    nums = _numeric_list(values)
    if not nums:
        return None
    return ct.DoubleType(max(nums))


def cel_percentile(values: Any, q: Any) -> ct.DoubleType | None:
    """``percentile(values, q) -> number``: the *q*-th percentile (0–100).

    Uses **linear interpolation** between the two closest ranks (the
    numpy/pandas "linear" method), so ``percentile(xs, 50)`` is the median.
    Nulls are ignored. Returns CEL ``null`` for an empty/all-null list,
    malformed input, or a *q* outside ``[0, 100]`` (an out-of-range
    percentile is a misuse that should fail, not be clamped). Returns a
    double.
    """
    nums = _numeric_list(values)
    if not nums:
        return None
    if isinstance(q, (bool, ct.BoolType)) or not isinstance(q, (int, float)):
        return None
    q_value = float(q)
    if q_value < 0 or q_value > 100:  # noqa: PLR2004 - percentile domain is 0..100
        return None
    ordered = sorted(nums)
    if len(ordered) == 1:
        return ct.DoubleType(ordered[0])
    rank = (q_value / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    fraction = rank - low
    interpolated = ordered[low] + (ordered[high] - ordered[low]) * fraction
    return ct.DoubleType(interpolated)


def _make_now(clock: datetime) -> CelFunction:
    """Return a 0-arg ``now()`` bound to *clock* (pinned, not wall-clock).

    CEL has no nondeterministic builtins by design; ``now()`` is a
    Validibot injection. Binding it to a fixed instant (the run's
    ``started_at``) is what makes a time-relative assertion such as
    ``row.eventDate <= now()`` reproducible for a given run record. A
    naive *clock* is interpreted as UTC, matching :func:`_parse_iso8601`.
    """
    pinned = clock if clock.tzinfo is not None else clock.replace(tzinfo=UTC)
    pinned_ts = ct.TimestampType(pinned)

    def _now() -> ct.TimestampType:
        return pinned_ts

    return _now


# The V1 row helpers — the per-row inventory ADR-2026-05-26 pins for the
# Tabular Validator's row stage. Kept as a named group so the V1 inventory
# constant below does not drift when other stateless helpers are added.
_V1_ROW_HELPERS: dict[str, CelFunction] = {
    "is_iso8601": cel_is_iso8601,
    "parse_date": cel_parse_date,
    "is_finite": cel_is_finite,
}

# Scalar and aggregate helpers (documented in DEFAULT_HELPERS, used mainly
# by the future column-aggregate stage). These are stateless and safe, so
# they are bound everywhere alongside the row helpers. ``duration`` is
# deliberately absent: CEL already provides ``duration("3600s")`` natively,
# and binding a custom ``duration`` here would shadow that builtin.
_SCALAR_AND_AGGREGATE_HELPERS: dict[str, CelFunction] = {
    "is_int": cel_is_int,
    "abs": cel_abs,
    "round": cel_round,
    "mean": cel_mean,
    "sum": cel_sum,
    "min": cel_min,
    "max": cel_max,
    "percentile": cel_percentile,
}

# Every stateless helper — safe to share across runs and threads (no
# captured state) and bound onto every program regardless of context.
STATELESS_HELPERS: dict[str, CelFunction] = {
    **_V1_ROW_HELPERS,
    **_SCALAR_AND_AGGREGATE_HELPERS,
}

# The V1 helper inventory exactly (the four row helpers plus ``now``, which
# is bound only when a run clock is supplied — see
# :func:`build_cel_functions`). This is *deliberately* the V1 row set, not
# all stateless helpers, so it matches the ADR invariants block; a test
# pins it. It is no longer the source for the forms allowlist — that now
# derives from ``DEFAULT_HELPERS`` via ``CUSTOM_HELPER_NAMES`` so every
# documented helper is accepted at authoring time.
V1_CEL_HELPER_NAMES: frozenset[str] = frozenset(_V1_ROW_HELPERS) | {"now"}


def build_cel_functions(*, now: datetime | None = None) -> dict[str, CelFunction]:
    """Build the function map to bind onto a CEL program.

    Always includes every stateless helper — the V1 row helpers
    (``is_iso8601``, ``parse_date``, ``is_finite``) plus the scalar and
    aggregate helpers (``is_int``, ``abs``, ``round``, ``mean``, ``sum``,
    ``min``, ``max``, ``percentile``). ``now()`` is included **only** when a
    run clock is supplied: an expression that calls ``now()`` without a
    pinned clock then fails with an unknown-function error rather than
    silently reading the wall clock, which would break the determinism
    contract. Callers that need ``now()`` (notably the Tabular Validator,
    which passes ``run.started_at``) opt in by supplying *now*.
    """
    functions = dict(STATELESS_HELPERS)
    if now is not None:
        functions["now"] = _make_now(now)
    return functions
