"""Untrusted-input hardening + resource limits (ADR-2026-07-01, D8).

The Schematron *packs* are curated and trusted; the **submitted XML is not**.
This module owns the Django-side half of the D8 posture:

- **The hardened submission parse guard** (:func:`assert_submission_is_safe_xml`)
  — the same ``defusedxml`` posture as ``validations/xml_utils.py``
  (``forbid_dtd=True``; entity/external-reference rejection) plus size and
  depth caps. ``SchematronValidator.preprocess_submission()`` runs this
  *before dispatch*, so an XXE/billion-laughs/oversize payload is rejected
  in Django before any container is launched and before anything reaches
  Saxon. The backend re-applies the same guards container-side (defence in
  depth, mirroring how the SHACL backend re-clamps).
- **The D8 resource-limit table** — a Django-settings default and a hard
  ceiling per limit; operator settings are clamped to the ceilings
  (:func:`resolve_schematron_limits`), and the container re-clamps.

The Saxon-side lockdown (external functions off, ``doc()``/``document()``/
``unparsed-text()``/``collection()`` denied, no DTD/XInclude) is enforced in
the ``validibot-validator-backend-schematron`` image (Phase 3) — it cannot be
enforced from Django, so it is deliberately *not* represented here beyond the
contract documented in the ADR.

Exceeding a cap is an **engine error, not a validation failure** (D9):
"we refused to run because your file was too large" must never render as
"your invoice broke a rule".
"""

from __future__ import annotations

from dataclasses import dataclass

from defusedxml import ElementTree as SafeET
from defusedxml.common import DTDForbidden
from defusedxml.common import EntitiesForbidden
from defusedxml.common import ExternalReferenceForbidden
from django.conf import settings as django_settings

# ---------------------------------------------------------------------------
# D8 resource-limit table: (default, hard max) per limit. Operator settings
# (SCHEMATRON_* names) are clamped to the hard maxima — a misconfigured
# setting can never widen the safety net.
# ---------------------------------------------------------------------------

# Matches xml_utils' DEFAULT_MAX_SIZE_BYTES; invoices are tiny, but batch/UBL
# packages can be larger.
DEFAULT_MAX_INPUT_BYTES = 10_000_000
HARD_MAX_INPUT_BYTES = 50_000_000

# Recursion/stack guard, as xml_utils.
DEFAULT_MAX_INPUT_DEPTH = 200
HARD_MAX_INPUT_DEPTH = 500

# Wall-clock for the XSLT transform; a pack over a pathological document must
# not hang the job.
DEFAULT_XSLT_TIMEOUT_SECONDS = 60
HARD_MAX_XSLT_TIMEOUT_SECONDS = 300

# Container memory ceiling for Saxon; OOM → engine error (D9), not a hang.
DEFAULT_MAX_MEMORY_MB = 512
HARD_MAX_MEMORY_MB = 1024

# Cap surfaced findings (D10) so a wildly non-conforming document can't emit
# tens of thousands of rows. Truncation is always surfaced, never silent.
DEFAULT_MAX_FINDINGS = 500
HARD_MAX_FINDINGS = 2000


class SchematronSecurityError(ValueError):
    """Raised when a submission violates the hardened-XML guard.

    The message is user-facing: the validator converts it into a clear
    pre-dispatch ValidationError finding rather than a container launch.
    """


@dataclass(frozen=True)
class SchematronLimits:
    """The resolved D8 limits shipped to the container in the input envelope.

    Already clamped to the hard ceilings Django-side; the container treats
    them as authoritative but re-clamps defensively (the SHACL pattern).
    """

    max_input_bytes: int
    max_input_depth: int
    xslt_timeout_seconds: int
    max_memory_mb: int
    max_findings: int


def resolve_schematron_limits() -> SchematronLimits:
    """Read the operator's ``SCHEMATRON_*`` settings, clamped to hard caps."""
    return SchematronLimits(
        max_input_bytes=_setting_int(
            "SCHEMATRON_MAX_INPUT_BYTES",
            DEFAULT_MAX_INPUT_BYTES,
            HARD_MAX_INPUT_BYTES,
        ),
        max_input_depth=_setting_int(
            "SCHEMATRON_MAX_INPUT_DEPTH",
            DEFAULT_MAX_INPUT_DEPTH,
            HARD_MAX_INPUT_DEPTH,
        ),
        xslt_timeout_seconds=_setting_int(
            "SCHEMATRON_XSLT_TIMEOUT_SECONDS",
            DEFAULT_XSLT_TIMEOUT_SECONDS,
            HARD_MAX_XSLT_TIMEOUT_SECONDS,
        ),
        max_memory_mb=_setting_int(
            "SCHEMATRON_MAX_MEMORY_MB",
            DEFAULT_MAX_MEMORY_MB,
            HARD_MAX_MEMORY_MB,
        ),
        max_findings=_setting_int(
            "SCHEMATRON_MAX_FINDINGS",
            DEFAULT_MAX_FINDINGS,
            HARD_MAX_FINDINGS,
        ),
    )


def assert_submission_is_safe_xml(
    content: str | bytes | None,
    *,
    max_bytes: int | None = None,
    max_depth: int | None = None,
) -> None:
    """Reject a submission that violates the hardened-XML posture (D8a).

    Runs the same guards as ``validations/xml_utils.py`` without building the
    dict representation (Schematron ships the raw document to the container;
    Django only needs the safety check):

    - non-empty, within the size cap;
    - parses under ``defusedxml`` with ``forbid_dtd=True`` (blocks XXE,
      external entities, entity-expansion bombs, and DTDs outright);
    - nesting depth within the cap (recursion guard).

    Raises:
        SchematronSecurityError: With a user-facing message on any violation.
    """
    limits = resolve_schematron_limits()
    effective_max_bytes = max_bytes if max_bytes is not None else limits.max_input_bytes
    effective_max_depth = max_depth if max_depth is not None else limits.max_input_depth

    if content is None or not str(content).strip():
        raise SchematronSecurityError("The submission is empty — expected XML.")

    raw = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw) > effective_max_bytes:
        raise SchematronSecurityError(
            f"The XML submission is too large "
            f"({len(raw):,} bytes > {effective_max_bytes:,} bytes).",
        )

    try:
        root = SafeET.fromstring(raw, forbid_dtd=True)
    except (EntitiesForbidden, ExternalReferenceForbidden, DTDForbidden) as exc:
        raise SchematronSecurityError(
            "The XML submission contains forbidden constructs "
            "(entities, external references, or DTD declarations).",
        ) from exc
    except SafeET.ParseError as exc:
        raise SchematronSecurityError(
            f"The submission is not well-formed XML: {exc}",
        ) from exc

    _assert_depth_within(root, effective_max_depth)


def _assert_depth_within(root, max_depth: int) -> None:
    """Iterative depth check (no recursion, so the guard can't blow the stack)."""
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        if depth > max_depth:
            raise SchematronSecurityError(
                f"The XML submission nests deeper than the maximum "
                f"({max_depth} levels) — it may be malformed or malicious.",
            )
        stack.extend((child, depth + 1) for child in element)


def _setting_int(name: str, default: int, hard_max: int) -> int:
    """Read a positive int Django setting, clamped to a hard maximum.

    Same contract as the SHACL launcher's clamp helper: non-numeric or
    non-positive values fall back to the default; values above the ceiling
    are clamped down, never honoured.
    """
    try:
        value = int(getattr(django_settings, name, default))
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, hard_max)
