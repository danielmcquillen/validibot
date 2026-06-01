"""Finding presentation helpers — the failing-row summary shown with a finding.

Some validators (the Tabular Validator today) aggregate a bulk failure into a
single finding rather than one finding per failing row: the finding carries the
total failure ``count`` plus a capped list of example row numbers
(``sample_rows``) in its ``meta``. See the Tabular Validator's ``native.py`` /
``row_eval.py`` for where those are produced and ``DEFAULT_REPORT_MAX_EXAMPLES``
for the cap.

This module turns that ``meta`` into something user-facing, in **one** place so
the web UI and the API never disagree:

* :func:`summarize_failed_rows` returns the *structured* form
  (``sample_rows`` / ``count`` / ``truncated``) — what the API hands clients so
  they can render it however they like.
* :func:`format_failed_rows` returns the *human* string built from that summary
  (e.g. ``"rows 1, 2, 4 (showing first 100 of 3,412)"``) — what the template tag
  drops next to the message.

Both read ``meta`` defensively, so they are a no-op for findings that don't
carry row examples (JSON Schema, XML, SHACL, …): no ``sample_rows`` key means
``None`` / ``""``, and nothing renders.
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext as _


def summarize_failed_rows(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a structured failing-row summary from a finding's ``meta``.

    Args:
        meta: A finding's ``meta`` dict (or ``None``). Only ``sample_rows`` (a
            list of 1-based row numbers) and ``count`` (the *total* number of
            failing rows, which may exceed the sample) are read.

    Returns:
        ``{"sample_rows": [...], "count": int, "truncated": bool}`` when the
        finding carries row examples, else ``None``. ``truncated`` is ``True``
        when the true ``count`` exceeds the number of examples kept — i.e. the
        sample is only the first slice of a larger failure set.
    """
    if not meta:
        return None
    sample_rows = meta.get("sample_rows")
    if not sample_rows:
        return None
    rows = list(sample_rows)
    # ``count`` is the authoritative total; fall back to the sample length when
    # a producer omitted it (so we never claim a truncation we can't prove).
    count = meta.get("count")
    if not isinstance(count, int) or count < len(rows):
        count = len(rows)
    return {
        "sample_rows": rows,
        "count": count,
        "truncated": count > len(rows),
    }


def format_failed_rows(meta: dict[str, Any] | None) -> str:
    """Return a human-readable failing-row line for a finding, or ``""``.

    Builds on :func:`summarize_failed_rows`. When the sample is the whole set
    the string is just ``"row #s: 1, 2, 4"``; when it was capped it makes the
    truncation explicit — ``"row #s: 1, 2, … (showing first 100 of 3,412)"`` — so
    the reader knows there are more failures than the ones listed.

    The ``row #s:`` label is deliberate: a bare ``"rows 2"`` reads like a *count*
    ("two rows failed") rather than "row number 2". Naming it as a list of row
    numbers removes that ambiguity for single-row failures.
    """
    summary = summarize_failed_rows(meta)
    if summary is None:
        return ""
    rows = ", ".join(str(row) for row in summary["sample_rows"])
    if summary["truncated"]:
        return _("row #s: %(rows)s (showing first %(shown)s of %(total)s)") % {
            "rows": rows,
            "shown": len(summary["sample_rows"]),
            "total": summary["count"],
        }
    return _("row #s: %(rows)s") % {"rows": rows}
