"""Django-side assembly of the typed Schematron container inputs.

Mirrors ``validators/shacl/launch.py``: the container has no database and no
access to the community repo's vendored pack files, so everything it needs is
resolved here and shipped in the typed ``SchematronInputs`` (ADR-2026-07-01,
D4b). Unlike SHACL — which inlines merged shapes *text* — Schematron ships an
**artefact reference**: the dispatch layer stages the vendored, checksum-
verified XSLT to run-scoped storage (Cloud Run → ``gs://…``; local Docker →
container-mounted ``file://…``, see ``staging.py``) and this module packages
the staged URI together with the pinned checksums so the container can verify
before executing.

Pack resolution follows the library attachment (D5): each vendored pack is a
library ``Validator`` row whose ``default_ruleset`` is the global pack
``Ruleset`` carrying the pin — resolution reads ``validator.default_ruleset``
(see ``packs.resolve_pack_for_validator``), exactly as ``resolve_shacl_inputs``
reads a library validator's default ruleset for shapes. The step's own
ruleset is the author's assertion surface and plays no part here.

NOTE: this module imports ``validibot_shared.schematron`` (shared >= 0.11.0).
It is intentionally NOT imported by ``config.py``/``validator.py`` (which
must be importable at app boot against older shared releases) — only the
dispatch layer and the launch tests import it. The shared-free half of
resolution (registry lookups, pointer verification) lives in ``packs.py``.
"""

from __future__ import annotations

from typing import Any

from validibot_shared.schematron.envelopes import SchematronInputs

from validibot.validations.validators.schematron.packs import (
    SchematronPackResolutionError,
)
from validibot.validations.validators.schematron.packs import resolve_pack_for_ruleset
from validibot.validations.validators.schematron.packs import resolve_pack_for_validator
from validibot.validations.validators.schematron.security import (
    resolve_schematron_limits,
)

__all__ = [
    "SchematronPackResolutionError",
    "resolve_pack_for_ruleset",
    "resolve_pack_for_validator",
    "resolve_schematron_inputs",
]


def resolve_schematron_inputs(
    *,
    validator: Any,
    artifact_uri: str,
) -> SchematronInputs:
    """Build the typed ``SchematronInputs`` for the container.

    Args:
        validator: The step's library ``Validator`` row (its
            ``default_ruleset`` carries the pack pointer, D5).
        artifact_uri: The *staged*, container-visible URI for the compiled
            XSLT — ``gs://…`` on Cloud Run, ``file://…`` for local Docker —
            produced by ``staging.verified_pack_artifact_path`` + the
            dispatch layer's delivery. The container fetches this URI and
            verifies ``artifact_sha256`` before executing (D4b); it never
            reads a Django package path.

    Raises:
        SchematronPackResolutionError: If the validator doesn't resolve to a
            vetted pack (see :func:`packs.resolve_pack_for_validator`).
    """
    pack = resolve_pack_for_validator(validator)
    limits = resolve_schematron_limits()

    return SchematronInputs(
        pack_id=pack.id,
        pack_version=pack.version,
        artifact_uri=artifact_uri,
        artifact_sha256=pack.artifact_sha256,
        source_sha256=pack.source_sha256,
        query_binding=pack.query_binding,
        engine=pack.engine,
        max_input_bytes=limits.max_input_bytes,
        max_input_depth=limits.max_input_depth,
        xslt_timeout_seconds=limits.xslt_timeout_seconds,
        max_memory_mb=limits.max_memory_mb,
        max_findings=limits.max_findings,
    )
