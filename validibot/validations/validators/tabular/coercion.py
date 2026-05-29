"""Deterministic, locale-free coercion of string cells to typed values.

The reader produces an all-string dataframe; coercion to declared types is a
separate, explicit step so it stays deterministic (ADR-2026-05-26: "Read
columns as strings; coerce per declared types with explicit, locale-independent
parsing"). This module is the single place that coercion happens, shared by
native structured validation and (later) row-stage CEL value binding, so the
two never disagree about what a cell *is*.

Every coercion is locale-free: numbers use ``.`` as the decimal separator with
no thousands grouping, and dates are ISO 8601 only. Two operators on two
machines coerce the same cell to the same value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.cel_helpers import _parse_iso8601

if TYPE_CHECKING:
    # Only used in a local annotation; ``from __future__ import annotations``
    # keeps it a string at runtime, so the import is type-only.
    from datetime import datetime

# Boolean spellings accepted by ``type=boolean`` coercion. Matches Table
# Schema's default true/false value sets (plus the common ``1``/``0``).
_TRUE_VALUES: frozenset[str] = frozenset({"true", "True", "TRUE", "1"})
_FALSE_VALUES: frozenset[str] = frozenset({"false", "False", "FALSE", "0"})


@dataclass(frozen=True)
class Coerced:
    """The outcome of coercing one cell.

    Exactly one of three states holds:

    - **null** (``is_null=True``, ``ok=True``): the cell was empty (``""``).
    - **value** (``is_null=False``, ``ok=True``): ``value`` holds the coerced
      Python value.
    - **type error** (``ok=False``): a non-empty cell that could not be coerced
      to the declared type.

    Keeping "empty" distinct from "type error" matters: an empty cell is a
    *nullability* question (does ``required`` allow it?), while an uncoercible
    non-empty cell is a *type* violation. They produce different findings.
    """

    ok: bool
    is_null: bool
    value: Any


_NULL = Coerced(ok=True, is_null=True, value=None)


def _coerce_number(raw: str) -> Coerced:
    try:
        return Coerced(ok=True, is_null=False, value=float(raw))
    except ValueError:
        return Coerced(ok=False, is_null=False, value=None)


def _coerce_integer(raw: str) -> Coerced:
    try:
        # base 10 only; "5.0" and "5.5" are rejected as non-integers, and a
        # locale-grouped "1,000" raises — both intentional.
        return Coerced(ok=True, is_null=False, value=int(raw))
    except ValueError:
        return Coerced(ok=False, is_null=False, value=None)


def _coerce_boolean(raw: str) -> Coerced:
    if raw in _TRUE_VALUES:
        return Coerced(ok=True, is_null=False, value=True)
    if raw in _FALSE_VALUES:
        return Coerced(ok=True, is_null=False, value=False)
    return Coerced(ok=False, is_null=False, value=None)


def _coerce_temporal(raw: str) -> Coerced:
    parsed: datetime | None = _parse_iso8601(raw)
    if parsed is None:
        return Coerced(ok=False, is_null=False, value=None)
    return Coerced(ok=True, is_null=False, value=parsed)


def coerce_cell(raw: str, field_type: str) -> Coerced:
    """Coerce a raw cell string to ``field_type``.

    An empty string is *null* regardless of type (nullability is decided by the
    ``required`` constraint, not here). ``string`` always succeeds. ``number``
    and ``integer`` parse locale-free; ``boolean`` accepts a fixed set of
    spellings; ``date``/``datetime`` accept ISO 8601 only. Anything that fails
    is a type error (``ok=False``), which native validation reports as
    ``tabular.type_error``.
    """
    if raw == "":
        return _NULL
    if field_type == "string":
        return Coerced(ok=True, is_null=False, value=raw)
    if field_type == "number":
        return _coerce_number(raw)
    if field_type == "integer":
        return _coerce_integer(raw)
    if field_type == "boolean":
        return _coerce_boolean(raw)
    if field_type in {"date", "datetime"}:
        return _coerce_temporal(raw)
    # Unknown type (should not occur — the schema parser maps unknowns to
    # ``string``); treat as string so we never crash on an unexpected type.
    return Coerced(ok=True, is_null=False, value=raw)
