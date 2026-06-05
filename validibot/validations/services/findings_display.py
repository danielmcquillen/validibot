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
  (e.g. ``"row numbers: 1, 2, 4 (showing first 100 of 3,412)"``) — what the template
  tag drops next to the message.

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
    the string is just ``"row numbers: 1, 2, 4"``; when it was capped it makes the
    truncation explicit — ``"row numbers: 1, 2, … (showing first 100 of 3,412)"`` — so
    the reader knows there are more failures than the ones listed.

    The ``row numbers:`` label is deliberate: a bare ``"rows 2"`` reads like a *count*
    ("two rows failed") rather than "row number 2". Naming it as a list of row
    numbers removes that ambiguity for single-row failures.
    """
    summary = summarize_failed_rows(meta)
    if summary is None:
        return ""
    rows = ", ".join(str(row) for row in summary["sample_rows"])
    if summary["truncated"]:
        return _("row numbers: %(rows)s (showing first %(shown)s of %(total)s)") % {
            "rows": rows,
            "shown": len(summary["sample_rows"]),
            "total": summary["count"],
        }
    return _("row numbers: %(rows)s") % {"rows": rows}


# ── Finding subject (P0: the specific instance a finding is about) ──────────
#
# A finding's ``path`` answers "which *property*"; its **subject** answers
# "which *instance*". For SHACL that subject is the offending focus node IRI
# (and optionally the offending value) — the single most useful thing for
# locating a violation in an RDF graph, which has no line numbers. The engine
# already captures it in ``meta`` (``shacl_focus_node`` / ``shacl_value``); this
# helper surfaces it generically so the UI/API can show "which one".
#
# ``_SUBJECT_META_KEYS`` is a *list* on purpose: it's the extension point. Other
# validators (JSON pointer instance, XML element, EnergyPlus object name) can
# opt in later by writing their own key here, with no template/serializer change.
# Until they do, this is a no-op for them — ``finding_subject`` returns ``None``
# and the UI renders exactly as before.
_SUBJECT_META_KEYS: tuple[str, ...] = ("shacl_focus_node",)
_SUBJECT_VALUE_META_KEYS: tuple[str, ...] = ("shacl_value",)


def _shorten_iri(iri: str) -> str:
    """Return the last segment of an IRI/CURIE for compact display.

    ``http://onuma.com/bldg-3593#GenericC_3210411`` → ``GenericC_3210411`` and
    a prefixed ``ex:EO_1`` → ``EO_1``. The full identifier is kept by the caller
    (e.g. a ``title`` attribute) so nothing is lost — this only trims the noisy
    namespace prefix that repeats on every row. Splitting on ``:`` as well as
    ``#``/``/`` is safe for full IRIs because the fragment/path separator always
    sits after the scheme colon, so ``#``/``/`` win there.
    """
    cut = max(iri.rfind("#"), iri.rfind("/"), iri.rfind(":"))
    if 0 <= cut < len(iri) - 1:
        return iri[cut + 1 :]
    return iri


def finding_subject(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the instance a finding is about, or ``None``.

    Reads ``meta`` defensively (no-op for findings without a subject key, e.g.
    JSON Schema / XML / tabular today). Returns::

        {"subject": <full IRI>, "subject_short": <last segment>, "value": <str|None>}

    ``value`` is the offending RDF term when the constraint reported one
    (``shacl_value``), else ``None``.
    """
    if not meta:
        return None
    subject = next((str(meta[k]) for k in _SUBJECT_META_KEYS if meta.get(k)), None)
    if not subject:
        return None
    value = next(
        (
            str(meta[k])
            for k in _SUBJECT_VALUE_META_KEYS
            if meta.get(k) not in (None, "")
        ),
        None,
    )
    return {
        "subject": subject,
        "subject_short": _shorten_iri(subject),
        "value": value,
    }


def abbreviate_iri(value: str | None) -> str:
    """Return the local name of an IRI for compact display; pass non-IRIs through.

    A SHACL finding's ``path`` is the ``sh:resultPath`` — the RDF *predicate* the
    failing constraint applies to — and RDF predicates are identified by full
    IRIs (e.g. ``http://data.ashrae.org/standard223#hasConnectionPoint``); they
    have no inherent short name. Repeated across a report, the namespace prefix
    is pure noise. This returns the local name (``hasConnectionPoint``) so the
    caller can show that with the full IRI on hover.

    Only IRI-shaped values are shortened. JSON pointers (``/items/0/name``) and
    XPaths from the JSON/XML validators are returned unchanged — their full path
    *is* the useful information, so collapsing them would lose context.
    """
    if not value:
        return value or ""
    if value.startswith(("http://", "https://", "urn:")) or "#" in value:
        return _shorten_iri(value)
    return value


# ── Finding grouping (P1: collapse repeated rule violations) ────────────────


def group_step_findings(findings: Any) -> dict[str, Any]:
    """Group a step's findings for display: by severity, then by rule.

    Real validation runs repeat the *same* rule across many instances — an
    ASHRAE 223P building yields the same "An ``ElectricityOutlet`` shall have
    exactly one inlet" violation for dozens of outlets. A flat list buries the
    signal. This collapses each repeated rule into a single row carrying a
    count, while the instances that vary (the subjects) move into an
    expandable detail.

    The grouping is deliberately **generic and conservative** so it is safe for
    every validator, not just SHACL:

    * **Level 1 — severity.** Preserves the queryset's existing severity order
      (ERROR → WARNING → INFO) so the section headers match today's report.
    * **Level 2 — rule identity ``(code, message, path)``.** Two findings
      collapse only when all three match. For SHACL the focus *node* varies
      while ``(code, message, path)`` is constant, so a rule's instances
      collapse. For JSON Schema / XML the ``path`` (JSON pointer / XPath) is
      part of the identity, so findings at *different* locations stay separate —
      only genuine exact duplicates collapse. A single-member group renders
      exactly as it does today (no count badge), so a validator whose findings
      are all distinct sees **no change**.

    Returns a structure the template iterates directly::

        {
          "show_subject": bool,            # any finding carries a subject?
          "severities": [
            {
              "label": str,                # get_severity_display() of the section
              "groups": [
                {
                  "representative": finding,   # for badge / path / message
                  "count": int,
                  "members": [finding, ...],
                  "subjects": [...],           # finding_subject() per member
                  "failed_rows": str,          # tabular row summary for the rep, or ""
                },
                ...
              ],
            },
            ...
          ],
        }

    Grouping is presentation-only: callers pass the individual ``ValidationFinding``
    rows (the API and ``ValidationRunSummary`` keep counting them individually),
    and only the *rendering* collapses — so exact totals are never lost.
    """
    show_subject = False
    # Insertion-ordered: severity label → {"groups": {rule_key → group_dict}}.
    by_severity: dict[str, dict[str, Any]] = {}

    for finding in findings:
        meta = getattr(finding, "meta", None)
        subject = finding_subject(meta)
        if subject:
            show_subject = True

        severity_label = finding.get_severity_display()
        section = by_severity.setdefault(severity_label, {"groups": {}})

        rule_key = (
            getattr(finding, "code", "") or "",
            getattr(finding, "message", "") or "",
            getattr(finding, "path", "") or "",
        )
        group = section["groups"].get(rule_key)
        if group is None:
            group = {"representative": finding, "members": [], "subjects": []}
            section["groups"][rule_key] = group
        group["members"].append(finding)
        if subject:
            group["subjects"].append(subject)

    severities: list[dict[str, Any]] = []
    for label, section in by_severity.items():
        groups_out: list[dict[str, Any]] = []
        for group in section["groups"].values():
            rep = group["representative"]
            groups_out.append(
                {
                    "representative": rep,
                    "count": len(group["members"]),
                    "members": group["members"],
                    "subjects": group["subjects"],
                    "failed_rows": format_failed_rows(getattr(rep, "meta", None)),
                },
            )
        severities.append({"label": label, "groups": groups_out})

    return {"show_subject": show_subject, "severities": severities}
