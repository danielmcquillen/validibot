"""Evidence-bundle export views.

ADR-2026-04-27 Phase 4 Session C/1: operator-facing endpoint that
downloads the canonical-JSON manifest for a completed validation
run.

Why this is just the manifest, not a tarball
============================================

The ADR's Session C end-state is a multi-file bundle (manifest +
signature + optional input/output) packaged as a tarball. That
shape requires:

1. The pro-side credential schema to grow a ``manifest_hash`` field
   so the signed credential can sit alongside the manifest in the
   bundle and verifiers can re-derive the link.
2. A retention-aware decision about whether input / output bytes
   ride along (we already have the policy infrastructure from
   Session B for the manifest's own redactions; the bundle-level
   inclusion list is a parallel concern).

Both are deferred to Session C/2. Session C/1 ships the most
useful concrete artefact today: the canonical-JSON manifest as a
download. Operators can save it, pass it to external systems, or
hash-verify it manually. The API surface is intentionally thin so
Session C/2 can extend without breaking it.
"""

from __future__ import annotations

import logging

from django.http import FileResponse
from django.http import Http404
from django.utils.translation import gettext_lazy as _
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.detail import View

from validibot.validations.models import RunEvidenceArtifactAvailability
from validibot.validations.views.runs import ValidationRunAccessMixin

logger = logging.getLogger(__name__)


class EvidenceManifestDownloadView(
    ValidationRunAccessMixin,
    SingleObjectMixin,
    View,
):
    """Download the run's evidence manifest as ``manifest.json``.

    Inherits from :class:`ValidationRunAccessMixin` so the queryset
    is already permission-gated identically to the run-detail view —
    the user must have viewing rights on the run for this URL to
    resolve. No additional permission check needed here.

    Response:
    - **200**: streamed ``manifest.json`` (Content-Type
      ``application/json``, attachment disposition).
    - **404**: run accessible but has no GENERATED artifact
      (FAILED / missing). Returns a 404 rather than a 5xx because
      the absence is part of the run's expected lifecycle (e.g.
      manifest stamper hasn't run yet for an in-flight run, or the
      stamp failed and the operator needs to re-finalise).

    No-cache headers are set so a re-export after a re-stamp picks
    up the new bytes.
    """

    context_object_name = "run"

    def get_queryset(self):
        return self.get_base_queryset()

    def get(self, request, *args, **kwargs):
        run = self.get_object()
        try:
            artifact = run.evidence_artifact
        except Exception as exc:
            logger.debug(
                "Evidence download requested but run has no artifact",
                extra={"run_id": str(run.id), "exc": str(exc)},
            )
            msg = _("This run has no evidence manifest yet.")
            raise Http404(msg) from None

        if artifact.availability != RunEvidenceArtifactAvailability.GENERATED:
            logger.info(
                "Evidence download requested for non-GENERATED artifact",
                extra={
                    "run_id": str(run.id),
                    "availability": artifact.availability,
                },
            )
            msg = _("This run's evidence manifest is unavailable.")
            raise Http404(msg)

        if not artifact.manifest_path:
            msg = _("This run's evidence manifest has no stored bytes.")
            raise Http404(msg)

        # Stream the file rather than loading into memory — manifests
        # are typically a few KB but the streaming path is the safer
        # default and aligns with how Django serves uploaded files.
        artifact.manifest_path.open("rb")
        response = FileResponse(
            artifact.manifest_path,
            as_attachment=True,
            filename="manifest.json",
            content_type="application/json",
        )
        # Re-exports after re-stamp must surface the latest bytes.
        # The artifact's ``manifest_hash`` is the canonical identity;
        # if a verifier wants to confirm freshness, they re-fetch
        # and compare hashes.
        response["Cache-Control"] = "no-store, max-age=0"
        # Surface the manifest hash as a header so CLI consumers can
        # verify without parsing the body. ``X-`` prefix matches the
        # internal-extension convention used by the audit command's
        # JSON schema header pattern.
        response["X-Validibot-Manifest-Sha256"] = artifact.manifest_hash
        response["X-Validibot-Schema-Version"] = artifact.schema_version
        return response


__all__ = ["EvidenceManifestDownloadView"]
