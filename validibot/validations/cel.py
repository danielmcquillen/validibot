from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    # Only referenced in the ``description`` annotation; ``from __future__
    # import annotations`` makes that hint a string, so this is type-only.
    from django.utils.functional import Promise


@dataclass(frozen=True)
class CelHelper:
    """Describes a single CEL helper function exposed to assertions."""

    name: str
    signature: str
    return_type: str
    # ``gettext_lazy`` returns a lazy proxy (``Promise``), not a ``str``;
    # accept both so the translated descriptions below type-check.
    description: str | Promise


DEFAULT_HELPERS: dict[str, CelHelper] = {
    "has": CelHelper(
        name="has",
        signature="has(value)",
        return_type="bool",
        description=_("Returns true when the value is not null/empty."),
    ),
    "is_int": CelHelper(
        name="is_int",
        signature="is_int(value)",
        return_type="bool",
        description=_(
            "Returns true when the value is an integral number "
            "(2 and 2.0 are integral; 2.5, booleans, NaN, and non-numbers "
            "are not).",
        ),
    ),
    "percentile": CelHelper(
        name="percentile",
        signature="percentile(values, q)",
        return_type="number",
        description=_(
            "The q-th percentile (q in 0–100) of a numeric list by linear "
            "interpolation; ignores nulls. Null for an empty list or an "
            "out-of-range q. Returns a double.",
        ),
    ),
    "mean": CelHelper(
        name="mean",
        signature="mean(values)",
        return_type="number",
        description=_(
            "Arithmetic mean of a numeric list (ignores nulls); null for an "
            "empty list. Returns a double.",
        ),
    ),
    "sum": CelHelper(
        name="sum",
        signature="sum(values)",
        return_type="number",
        description=_(
            "Sum of a numeric list (ignores nulls); 0 for an empty list. "
            "Returns a double.",
        ),
    ),
    "max": CelHelper(
        name="max",
        signature="max(values)",
        return_type="number",
        description=_(
            "Largest value in a numeric list (ignores nulls); null for an "
            "empty list. Returns a double.",
        ),
    ),
    "min": CelHelper(
        name="min",
        signature="min(values)",
        return_type="number",
        description=_(
            "Smallest value in a numeric list (ignores nulls); null for an "
            "empty list. Returns a double.",
        ),
    ),
    "abs": CelHelper(
        name="abs",
        signature="abs(value)",
        return_type="number",
        description=_(
            "Absolute value of a number (preserves int vs. double); null "
            "for a non-number.",
        ),
    ),
    "round": CelHelper(
        name="round",
        signature="round(value, digits)",
        return_type="number",
        description=_(
            "Round a number to a number of decimal places (digits defaults "
            "to 0) using round-half-to-even. Returns a double.",
        ),
    ),
    "duration": CelHelper(
        name="duration",
        signature="duration(string)",
        return_type="duration",
        description=_(
            'CEL built-in: parse a duration string such as "3600s" or '
            '"1h30m" into a duration value. (Provided by CEL itself, not a '
            "Validibot helper, so it is not bound in cel_helpers.)",
        ),
    ),
    # ── V1 Tabular Validator helpers (ADR-2026-05-26) ──────────────────
    # is_iso8601/parse_date/is_finite/now were the first helpers wired
    # end-to-end through all three registrations (this metadata, the forms
    # allowlist via CUSTOM_HELPER_NAMES, AND an executable binding in
    # cel_helpers.py). The scalar/aggregate helpers above are now bound the
    # same way; ``has`` (a CEL macro) and ``duration`` (a CEL built-in) are
    # intentionally NOT bound — celpy provides them, and binding a custom
    # ``duration`` would shadow the built-in.
    "is_iso8601": CelHelper(
        name="is_iso8601",
        signature="is_iso8601(value)",
        return_type="bool",
        description=_(
            "Returns true when the string is a valid ISO 8601 date or datetime.",
        ),
    ),
    "parse_date": CelHelper(
        name="parse_date",
        signature="parse_date(value)",
        return_type="timestamp",
        description=_(
            "Parses an ISO 8601 string into a timestamp (null if "
            "unparseable); for string-typed columns.",
        ),
    ),
    "is_finite": CelHelper(
        name="is_finite",
        signature="is_finite(value)",
        return_type="bool",
        description=_(
            "Returns true when the value is a finite number (not NaN or infinity).",
        ),
    ),
    "now": CelHelper(
        name="now",
        signature="now()",
        return_type="timestamp",
        description=_(
            "The run's pinned evaluation time (run.started_at); "
            "deterministic for the run, never the wall clock.",
        ),
    ),
}


# Canonical set of Validibot custom-helper function names — the single
# source of truth for every authoring-time CEL identifier allowlist.
#
# These names had been hand-duplicated across four allowlists (two in
# ``validations/forms.py``, one in ``validations/views/rules.py``, and
# ``RESERVED_CEL_NAMES`` in ``validations/services/signal_resolution.py``).
# That duplication is exactly the "registration drift" failure mode
# ADR-2026-05-26 calls out: adding ``is_iso8601`` etc. to one copy left the
# others rejecting it as an unknown identifier at save time. Deriving every
# allowlist from this one set keeps them in lockstep — a helper added to
# ``DEFAULT_HELPERS`` is automatically accepted everywhere CEL is authored.
CUSTOM_HELPER_NAMES: frozenset[str] = frozenset(DEFAULT_HELPERS)
