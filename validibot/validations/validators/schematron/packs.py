"""Curated Schematron rule-pack registry (ADR-2026-07-01, decision D5).

A **rule pack** is a versioned, vetted Schematron artefact (e.g. the EN 16931
business rules, or the Peppol BIS Billing 3.0 rules) that Validibot executes
against XML submissions. Packs are *curated, version-pinned allowlist entries*
— never arbitrary user uploads. Arbitrary Schematron compiles to arbitrary
XSLT, i.e. code execution, so the security model is: only artefacts declared
here (with matching checksums) may ever be staged to the validator backend.

This module is the **code-reviewed source of truth for what is permitted**:

- Each :class:`SchematronPack` pins an exact upstream release: the ``.sch``
  source checksum, the compiled-XSLT checksum actually executed, the query
  binding, and the engine required to run it.
- ``Ruleset.clean()`` validates that any ``SCHEMATRON`` ruleset points at a
  pack registered here with matching checksums — a hand-crafted ``Ruleset``
  row cannot smuggle in an un-vetted artefact.
- The Phase 3 vendoring management command populates the pinned entries (and
  the artefact files); until packs are vendored the registry is empty and the
  step-config form offers nothing.

Pack lifecycle rules (D5): pins are immutable — a new upstream release is a
*new* registry entry, never an in-place mutation. Superseded entries are
flagged ``deprecated`` (with ``superseded_by``), not removed, so existing
workflows keep resolving the exact bytes they were authored against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field

logger = logging.getLogger(__name__)


class SchematronPackResolutionError(ValueError):
    """Raised when something does not resolve to a vetted, checksum-true pack.

    Used across the resolution chain — registry lookups, ruleset pointer
    resolution (``launch.py``), and artefact staging (``staging.py``). The
    invariant it protects: Validibot never executes a Schematron artefact
    that isn't pinned in this registry with matching checksums.
    """


# Query bindings a pack may declare. The official EN 16931 / Peppol artefacts
# are xslt2; xslt1 exists for the tiny illustrative test fixtures only.
QUERY_BINDING_XSLT1 = "xslt1"
QUERY_BINDING_XSLT2 = "xslt2"
VALID_QUERY_BINDINGS = frozenset({QUERY_BINDING_XSLT1, QUERY_BINDING_XSLT2})

# Engines a pack may require. SaxonC-HE is the chosen production engine
# (ADR-2026-07-01 D4); lxml's isoschematron XSLT-1.0 support is a test-only
# helper and never a production runtime.
ENGINE_SAXONC_HE = "saxonc-he"
ENGINE_LXML_XSLT1 = "lxml-xslt1"


@dataclass(frozen=True)
class SchematronPack:
    """One pinned, vetted Schematron rule-pack release.

    Mirrors the descriptor table in ADR-2026-07-01 D5. The ``artifact`` path
    is relative to the community repo root and points at the *compiled XSLT*
    that is staged (with checksum verification) to the validator backend per
    run — packs are never baked into the backend image.

    ``rule_doc_url_template`` builds the D10 deep link from a native rule id
    to the publisher's own rule documentation, e.g.
    ``"https://docs.peppol.eu/poacc/billing/3.0/rules/#{rule_id}"``. Empty
    means no deep link is available for this pack.
    """

    id: str
    title: str
    version: str
    syntax: str  # "ubl" | "cii" | domain-specific
    source_url: str
    license: str
    query_binding: str
    artifact: str  # repo-relative path to the pinned compiled XSLT
    source_sha256: str  # sha256 of the pinned .sch source
    artifact_sha256: str  # sha256 of the compiled XSLT actually executed
    engine: str = ENGINE_SAXONC_HE
    rule_doc_url_template: str = ""
    deprecated: bool = False
    superseded_by: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """Registry key — (pack id, exact version)."""
        return (self.id, self.version)

    def rule_url(self, rule_id: str) -> str:
        """Deep link to the publisher's documentation for ``rule_id`` (D10).

        Returns an empty string when the pack declares no URL template or the
        rule id is blank, so callers can simply skip the link.
        """
        if not self.rule_doc_url_template or not rule_id:
            return ""
        try:
            return self.rule_doc_url_template.format(rule_id=rule_id)
        except (KeyError, IndexError, ValueError):
            logger.warning(
                "Bad rule_doc_url_template for pack %s@%s",
                self.id,
                self.version,
            )
            return ""


# ---------------------------------------------------------------------------
# Registry
#
# Deliberately EMPTY until the Phase 3 vendoring management command lands the
# first pinned artefacts (en16931-ubl, peppol-bis-billing-ubl — see the ADR's
# D7 pack-boundary rule: the Peppol pack is the PEPPOL-EN16931-* layer only,
# never the bundled EN 16931 copy). Tests register temporary packs via
# ``register_pack()`` and clean up with ``unregister_pack()``.
# ---------------------------------------------------------------------------

_PACK_REGISTRY: dict[tuple[str, str], SchematronPack] = {}


def register_pack(pack: SchematronPack) -> None:
    """Register a pack descriptor, refusing duplicates and bad bindings.

    Raises:
        ValueError: If the (id, version) pair is already registered or the
            query binding is not a known value. Immutability of pins is the
            point — re-vendoring a version must fail loudly, never silently
            replace bytes an existing workflow depends on (D5 lifecycle).
    """
    if pack.query_binding not in VALID_QUERY_BINDINGS:
        msg = (
            f"Pack {pack.id}@{pack.version} declares unknown query binding "
            f"'{pack.query_binding}' (expected one of {sorted(VALID_QUERY_BINDINGS)})"
        )
        raise ValueError(msg)
    if pack.key in _PACK_REGISTRY:
        msg = (
            f"Pack {pack.id}@{pack.version} is already registered — pinned "
            f"pack versions are immutable (register a new version instead)"
        )
        raise ValueError(msg)
    _PACK_REGISTRY[pack.key] = pack


def unregister_pack(pack_id: str, version: str) -> None:
    """Remove a pack from the registry (test cleanup only)."""
    _PACK_REGISTRY.pop((pack_id, version), None)


def get_pack(pack_id: str, version: str) -> SchematronPack | None:
    """Look up the exact pinned pack for (id, version), or ``None``."""
    return _PACK_REGISTRY.get((pack_id, version))


def list_packs(*, include_deprecated: bool = False) -> list[SchematronPack]:
    """All registered packs, newest-version-last within an id.

    Deprecated entries are excluded by default; library/lifecycle tooling
    passes ``include_deprecated=True`` so an existing pin on a deprecated
    version still resolves (present-but-warned per D5).
    """
    packs = sorted(_PACK_REGISTRY.values(), key=lambda p: (p.id, p.version))
    if include_deprecated:
        return packs
    return [p for p in packs if not p.deprecated]


# ---------------------------------------------------------------------------
# Resolution — from DB rows to registry pins.
#
# Deliberately duck-typed (no Django model imports) and validibot_shared-free
# so BOTH dispatch paths can resolve packs cheaply; the typed-envelope
# assembly that needs validibot_shared lives in launch.py.
# ---------------------------------------------------------------------------


def resolve_pack_for_validator(validator: object) -> SchematronPack:
    """Resolve a library validator to its pinned, vetted pack descriptor.

    Reads the pack pointer from ``validator.default_ruleset`` (the global
    pack row the vendoring command materialised, ADR-2026-07-01 D5) and
    re-verifies it against this registry.

    Raises:
        SchematronPackResolutionError: If the validator has no
            ``default_ruleset`` or the row doesn't resolve to a registered,
            checksum-matching pack.
    """
    default_ruleset = getattr(validator, "default_ruleset", None)
    if default_ruleset is None:
        msg = (
            f"Schematron validator {getattr(validator, 'slug', validator)!r} "
            f"has no default_ruleset — pack validators must carry the "
            f"global pack row (run the vendoring command)."
        )
        raise SchematronPackResolutionError(msg)
    return resolve_pack_for_ruleset(default_ruleset)


def resolve_pack_for_ruleset(ruleset: object) -> SchematronPack:
    """Resolve a global pack ``Ruleset`` row to its registry descriptor.

    Re-verifies (defence in depth — ``Ruleset.clean()`` already enforced at
    save time) that the row's checksum snapshot still matches the registry
    pin, so a drifted registry or tampered row cannot silently swap the
    artefact a step executes.

    Raises:
        SchematronPackResolutionError: If the row is missing, carries no
            pack pointer, points at an unregistered pack, or its checksum
            snapshot disagrees with the registry pin.
    """
    if ruleset is None:
        msg = "Schematron pack resolution requires a pack ruleset row."
        raise SchematronPackResolutionError(msg)

    metadata = getattr(ruleset, "metadata", None) or {}
    pack_id = str(metadata.get("pack_id") or "").strip()
    pack_version = str(metadata.get("pack_version") or "").strip()
    ruleset_pk = getattr(ruleset, "pk", None)
    if not pack_id or not pack_version:
        msg = (
            f"Ruleset {ruleset_pk} has no pack_id/pack_version in metadata — "
            f"cannot resolve a Schematron pack."
        )
        raise SchematronPackResolutionError(msg)

    pack = get_pack(pack_id, pack_version)
    if pack is None:
        msg = (
            f"Schematron pack {pack_id}@{pack_version} (ruleset {ruleset_pk}) "
            f"is not in the vetted pack registry."
        )
        raise SchematronPackResolutionError(msg)

    snapshot_artifact_sha = str(metadata.get("pack_artifact_sha256") or "")
    if snapshot_artifact_sha and snapshot_artifact_sha != pack.artifact_sha256:
        msg = (
            f"Ruleset {ruleset_pk} pins artifact sha256 "
            f"{snapshot_artifact_sha[:12]}… but the registry pin for "
            f"{pack_id}@{pack_version} is {pack.artifact_sha256[:12]}… — "
            f"refusing to run a drifted artefact."
        )
        raise SchematronPackResolutionError(msg)

    return pack
