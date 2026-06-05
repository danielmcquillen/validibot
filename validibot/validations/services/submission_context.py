"""The single builder for the ``submission.`` assertion namespace.

Per ADR-2026-06-03b, ``submission.`` is the sixth CEL/assertion namespace. It
exposes the submission *envelope* — the submitter-provided fields plus a set of
server-derived facts — to every assertion, regardless of the submitted file
format. This is the property that makes a per-submission gate possible for
non-JSON submissions (RDF ``.ttl``/SHACL), where the ``s.*`` signal namespace
silently falls back to defaults because there is no JSON to resolve paths
against.

``build_submission_assertion_context`` is the *only* place the envelope is
assembled. All three readers consume its output so they can never drift:

* the CEL context builder — ``context["submission"] = build_...(run)``
  (``validators/base/base.py``);
* basic-assertion payload enrichment — ``payload["submission"] = build_...(run)``
  as a nested sub-dict (``validators/base/base.py``);
* the tests.

Trust is documented per field, not inferred from nesting (see the contract in
the ADR): the submitter-set fields (``name``, ``short_description``,
``metadata``, ``original_filename``) are UNTRUSTED; only the server-derived
facts (``file_type``, ``size``, ``uploaded_at``) are trustworthy for an
acceptance gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun


def build_submission_assertion_context(
    validation_run: ValidationRun | None,
) -> dict[str, Any]:
    """Build the ``submission`` envelope dict for assertion evaluation.

    The returned dict is consumed verbatim by both assertion engines:

    * CEL places it under ``context["submission"]``. The CEL evaluator runs
      ``celpy.json_to_cel`` over it, which converts the ``uploaded_at``
      ``datetime`` into a CEL ``timestamp`` automatically — so a trustworthy
      freshness rule like ``now() - submission.uploaded_at < duration("720h")``
      works without any special handling here.
    * Basic (non-CEL) assertions place the *same* dict under
      ``payload["submission"]`` as a nested sub-dict, and ``resolve_path``
      walks ``submission.metadata.deliverable`` exactly as CEL does. The
      ``uploaded_at`` value stays a ``datetime`` (a base class of celpy's
      ``TimestampType``), so the basic temporal operators compare it directly.

    Field sources (see the ADR contract table):

    - ``name``               — ``Submission.name``               (submitter-set)
    - ``short_description``   — ``ValidationRun.short_description`` (submitter-set;
      a *run* field, surfaced here as envelope context rather than minting a
      whole ``run.`` namespace for one value)
    - ``metadata``           — ``Submission.metadata`` (whole bag; submitter-set;
      nested access, e.g. ``submission.metadata.deliverable``)
    - ``original_filename``   — ``Submission.original_filename`` (submitter's own
      filename, basename-normalized — NOT trustworthy for gating)
    - ``file_type``          — ``Submission.file_type``          (server-derived)
    - ``size``               — ``Submission.size_bytes`` (integer **bytes**;
      server-derived)
    - ``uploaded_at``        — ``Submission.created`` (timezone-aware UTC;
      server-stamped at receive — the one trustworthy temporal fact)

    The envelope is **stable across content purge**: ``purge_content`` clears
    only the file bytes and preserves every field exposed here, so a rule on
    ``submission.metadata.deliverable`` keeps working against a purged
    submission. Only ``p`` / ``payload`` (the file content) disappears.

    Args:
        validation_run: The run being evaluated, or ``None``.

    Returns:
        The submission envelope as a plain dict. When there is no run or no
        submission (e.g. ``_build_cel_context`` invoked without a run context
        in a unit test, or a run whose submission was never attached) an empty
        dict ``{}`` is returned — never an exception. ``submission.<field>``
        then resolves the same way a missing signal does for each engine.
    """
    if validation_run is None:
        return {}
    submission = getattr(validation_run, "submission", None)
    if submission is None:
        return {}

    metadata = getattr(submission, "metadata", None)
    if not isinstance(metadata, dict):
        # JSONField defaults to {}, but guard against a legacy NULL or a
        # non-dict value so nested ``submission.metadata.<key>`` access has a
        # map to walk rather than raising.
        metadata = {}

    return {
        # ── Submitter-set (UNTRUSTED) ────────────────────────────────────
        "name": submission.name or "",
        # short_description lives on the RUN, not the Submission — surfaced
        # here as logical envelope context (see ADR; no separate run.* namespace).
        "short_description": getattr(validation_run, "short_description", "") or "",
        "metadata": metadata,
        "original_filename": submission.original_filename or "",
        # ── Server-derived (TRUSTWORTHY for gating) ──────────────────────
        "file_type": submission.file_type or "",
        # size_bytes is an integer count of bytes (BigIntegerField).
        "size": submission.size_bytes or 0,
        # created is timezone-aware UTC (TimeStampedModel); celpy converts it
        # to a CEL timestamp via json_to_cel on the CEL path.
        "uploaded_at": submission.created,
    }
