"""Pack-artefact staging: the single verification path (ADR-2026-07-01 D4b).

A curated pack's compiled XSLT is a **repo-vendored file** — not a
``WorkflowStepResource`` row — so neither the Cloud Run GCS-upload path nor
the local Docker workspace materialiser covers it. Both dispatchers call
:func:`verified_pack_artifact_path` and then deliver the returned file their
own way:

- **Cloud Run** uploads the bytes to run-scoped GCS and passes a ``gs://…``
  URI in the input envelope.
- **Local Docker** materialises the file into the per-run workspace (as a
  resource spec) and passes the container-mounted ``file://…`` URI.

Either way the checksum is verified **here, before staging** (an operator
whose checkout drifted from the pinned registry fails fast in Django), and
verified **again in the container** before execution (defence in depth —
the container never trusts what it fetched).

Deliberately import-light (no ``validibot_shared``): the docker dispatch
path touches this module for every Schematron run, and it must be safe to
import wherever the pack registry is.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

from validibot.validations.validators.schematron.packs import (
    SchematronPackResolutionError,
)

if TYPE_CHECKING:
    from validibot.validations.validators.schematron.packs import SchematronPack

# Canonical filename the artefact is staged under (both dispatchers), so the
# container's expectations and the run bundle layout stay uniform.
PACK_ARTIFACT_FILENAME = "pack.xslt"


def verified_pack_artifact_path(pack: SchematronPack) -> Path:
    """Return the vendored artefact's path after verifying its checksum.

    The path is resolved repo-relative (``settings.BASE_DIR``), because
    packs are vendored into the community repo (D5) — never uploaded, never
    baked into the backend image.

    Raises:
        SchematronPackResolutionError: If the file is missing or its sha256
            does not match the registry pin. A drifted checkout must fail
            fast here, not ship an unverified artefact to the container.
    """
    path = Path(settings.BASE_DIR) / pack.artifact
    if not path.is_file():
        msg = (
            f"Schematron pack artefact for {pack.id}@{pack.version} not "
            f"found at {path} — is the pack vendored in this checkout?"
        )
        raise SchematronPackResolutionError(msg)

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != pack.artifact_sha256:
        msg = (
            f"Schematron pack artefact {path} has sha256 {digest[:12]}… but "
            f"the registry pins {pack.artifact_sha256[:12]}… for "
            f"{pack.id}@{pack.version} — refusing to stage a drifted "
            f"artefact."
        )
        raise SchematronPackResolutionError(msg)

    return path
