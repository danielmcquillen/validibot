"""Authenticated downloads for a run's permanent evidence receipt.

The manifest endpoint returns the canonical ``manifest.json`` bytes. The bundle
endpoint wraps those bytes with the optional ``credential.jwt`` in a minimal
archive. Payload-retention expiry does not gate either endpoint because these
files are the permanent receipt, not retained input or output payloads.
"""

from __future__ import annotations

import logging

from django.http import FileResponse
from django.http import Http404
from django.http import HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.detail import View

from validibot.validations.models import RunEvidenceArtifact
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
    """Download the canonical permanent receipt as ``manifest.json``."""

    context_object_name = "run"

    def get_queryset(self):
        """Use the same organization-scoped access rules as the run view."""

        return self.get_base_queryset()

    def get(self, request, *args, **kwargs):
        """Stream a generated manifest regardless of payload-retention state."""

        run = self.get_object()
        try:
            artifact = run.evidence_artifact
        except RunEvidenceArtifact.DoesNotExist as exc:
            logger.debug(
                "Evidence download requested but run has no artifact",
                extra={"run_id": str(run.id), "exc": str(exc)},
            )
            raise Http404(_("This run has no evidence manifest yet.")) from None

        if artifact.availability != RunEvidenceArtifactAvailability.GENERATED:
            logger.info(
                "Evidence download requested for non-GENERATED artifact",
                extra={
                    "run_id": str(run.id),
                    "availability": artifact.availability,
                },
            )
            raise Http404(_("This run's evidence manifest is unavailable."))
        if not artifact.manifest_path:
            raise Http404(_("This run's evidence manifest has no stored bytes."))

        artifact.manifest_path.open("rb")
        response = FileResponse(
            artifact.manifest_path,
            as_attachment=True,
            filename="manifest.json",
            content_type="application/json",
        )
        response["Cache-Control"] = "no-store, max-age=0"
        response["X-Validibot-Manifest-Sha256"] = artifact.manifest_hash
        response["X-Validibot-Schema-Version"] = artifact.schema_version
        return response


class EvidenceBundleDownloadView(
    ValidationRunAccessMixin,
    SingleObjectMixin,
    View,
):
    """Download ``manifest.json`` and optional ``credential.jwt`` as tar.gz."""

    context_object_name = "run"

    def get_queryset(self):
        """Use the same organization-scoped access rules as the run view."""

        return self.get_base_queryset()

    def get(self, request, *args, **kwargs):
        """Return the permanent bundle regardless of payload-retention state."""

        run = self.get_object()
        try:
            bundle_bytes = EvidenceBundleBuilder.build(run)
        except BundleNotAvailableError as exc:
            logger.debug(
                "Evidence bundle requested but unavailable",
                extra={"run_id": str(run.id), "reason": str(exc)},
            )
            raise Http404(_("This run's evidence bundle is unavailable.")) from None

        artifact = run.evidence_artifact
        response = HttpResponse(bundle_bytes, content_type="application/gzip")
        response["Content-Disposition"] = (
            f'attachment; filename="evidence-{run.id}.tar.gz"'
        )
        response["Cache-Control"] = "no-store, max-age=0"
        response["X-Validibot-Manifest-Sha256"] = artifact.manifest_hash
        response["X-Validibot-Schema-Version"] = artifact.schema_version
        return response


__all__ = [
    "EvidenceBundleDownloadView",
    "EvidenceManifestDownloadView",
]
