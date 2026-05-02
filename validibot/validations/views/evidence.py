"""Evidence-bundle export views.

ADR-2026-04-27 Phase 4 Sessions C/1 + C/3: operator-facing
endpoints that download evidence for a completed validation run.

Two endpoints, two granularities:

- ``EvidenceManifestDownloadView`` (Session C/1): just the
  ``manifest.json`` bytes. Fast, single-file, useful when an
  operator only needs the canonical record for hashing or audit.
- ``EvidenceBundleDownloadView`` (Session C/3): a ``.tar.gz``
  bundle with ``manifest.json`` + (when pro is installed and a
  credential exists) ``manifest.sig`` + ``README.txt``. The
  bundle is what the verify flow (Session C/4) consumes.

Both endpoints share permission gating via
``ValidationRunAccessMixin``: if a user can see the run-detail
view, they can download both artefacts. No separate permission
needed (the bundle is just a structured view of the run that the
user could already read).

Future Session C/3 extension: when the bundle grows raw
``input/`` and ``output/`` bytes (deferred this session per the
trust ADR Session C/3 scoping), the builder gets retention-aware
inclusion logic. The endpoint signature stays the same.
"""

from __future__ import annotations

import logging

from django.http import FileResponse
from django.http import Http404
from django.http import HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.detail import View

from validibot.validations.models import RunEvidenceArtifactAvailability
from validibot.validations.services.evidence_bundle import BundleNotAvailableError
from validibot.validations.services.evidence_bundle import EvidenceBundleBuilder
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


class EvidenceBundleDownloadView(
    ValidationRunAccessMixin,
    SingleObjectMixin,
    View,
):
    """Download the run's evidence bundle as ``evidence-<run>.tar.gz``.

    Builds a deterministic ``.tar.gz`` containing ``manifest.json``,
    ``manifest.sig`` (when ``validibot-pro`` is installed and the
    run has a signed credential), and ``README.txt``. See
    :mod:`validibot.validations.services.evidence_bundle` for the
    full spec.

    Permission gating piggybacks on
    :class:`ValidationRunAccessMixin`, identically to the manifest
    endpoint — if you can see the run-detail page, you can download
    the bundle.

    Response:

    - **200**: ``application/gzip`` attachment with a
      ``evidence-<run-id>.tar.gz`` filename. Manifest hash and
      schema-version headers carry through so CLI consumers can
      sanity-check the bundle's manifest without untarring.
    - **404**: run accessible but has no ``GENERATED`` artifact.
      Mirrors the manifest endpoint's no-leak convention.
    """

    context_object_name = "run"

    def get_queryset(self):
        return self.get_base_queryset()

    def get(self, request, *args, **kwargs):
        run = self.get_object()
        try:
            bundle_bytes = EvidenceBundleBuilder.build(run)
        except BundleNotAvailableError as exc:
            logger.debug(
                "Evidence bundle requested but unavailable",
                extra={"run_id": str(run.id), "reason": str(exc)},
            )
            msg = _("This run's evidence bundle is unavailable.")
            raise Http404(msg) from None

        # Read artifact metadata for the response headers (no DB
        # round-trip — the row was just consulted by the builder).
        artifact = run.evidence_artifact

        response = HttpResponse(
            bundle_bytes,
            content_type="application/gzip",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="evidence-{run.id}.tar.gz"'
        )
        response["Cache-Control"] = "no-store, max-age=0"
        # Mirror the manifest endpoint's headers so a CLI consumer
        # using either endpoint sees the same bytes-of-truth markers.
        response["X-Validibot-Manifest-Sha256"] = artifact.manifest_hash
        response["X-Validibot-Schema-Version"] = artifact.schema_version
        return response


__all__ = [
    "EvidenceBundleDownloadView",
    "EvidenceManifestDownloadView",
]
