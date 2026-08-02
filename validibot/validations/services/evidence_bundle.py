"""Build the minimal downloadable evidence archive for a validation run.

The archive is transport, not another evidence document. It always contains
the canonical stored ``manifest.json`` and includes ``credential.jwt`` only
when the Pro signing layer issued one. Verification is the credential's
signature plus its ``manifestHash`` binding to the manifest bytes.

Raw payloads, generated prose, and a separate bundle descriptor are excluded.
The manifest's top-level digests identify retained or deleted payloads without
putting those payloads in the permanent bundle.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from typing import TYPE_CHECKING

from django.apps import apps

from validibot.validations.models import RunEvidenceArtifact
from validibot.validations.models import RunEvidenceArtifactAvailability

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun


class BundleNotAvailableError(Exception):
    """Raised when a run has no downloadable generated manifest."""


class EvidenceBundleBuilder:
    """Package a run's canonical manifest and optional signed credential."""

    @staticmethod
    def build(run: ValidationRun) -> bytes:
        """Return a gzipped tar containing the run's evidence members."""

        artifact = EvidenceBundleBuilder._require_generated_artifact(run)
        manifest_bytes = EvidenceBundleBuilder._read_manifest_bytes(artifact)
        credential_bytes = EvidenceBundleBuilder._read_credential_bytes(run)
        return EvidenceBundleBuilder._pack_tarball(
            manifest_bytes=manifest_bytes,
            credential_bytes=credential_bytes,
        )

    @staticmethod
    def _require_generated_artifact(run):
        """Return the run's downloadable evidence row or raise a clean error."""

        try:
            artifact = run.evidence_artifact
        except RunEvidenceArtifact.DoesNotExist as exc:
            msg = f"Run {run.id} has no evidence manifest yet."
            raise BundleNotAvailableError(msg) from exc

        if artifact.availability != RunEvidenceArtifactAvailability.GENERATED:
            msg = (
                f"Run {run.id} evidence artifact is in "
                f"{artifact.availability} state; bundle not buildable."
            )
            raise BundleNotAvailableError(msg)
        if not artifact.manifest_path:
            msg = f"Run {run.id} evidence artifact has no stored manifest bytes."
            raise BundleNotAvailableError(msg)
        return artifact

    @staticmethod
    def _read_manifest_bytes(artifact) -> bytes:
        """Read the exact canonical bytes that the manifest hash covers."""

        artifact.manifest_path.open("rb")
        try:
            return artifact.manifest_path.read()
        finally:
            artifact.manifest_path.close()

    @staticmethod
    def _read_credential_bytes(run) -> bytes | None:
        """Return the compact JWS when Pro issued a credential for the run."""

        if not apps.is_installed("validibot_pro"):
            return None

        from validibot_pro.credentials.models import IssuedCredential

        credential = (
            IssuedCredential.objects.filter(workflow_run=run)
            .order_by("-created")
            .first()
        )
        if credential is None or not credential.credential_jws:
            return None
        return credential.credential_jws.encode("ascii")

    @staticmethod
    def _pack_tarball(
        *,
        manifest_bytes: bytes,
        credential_bytes: bytes | None,
    ) -> bytes:
        """Pack only the fixed evidence members into a safe archive."""

        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            EvidenceBundleBuilder._add_member(
                archive,
                "manifest.json",
                manifest_bytes,
            )
            if credential_bytes is not None:
                EvidenceBundleBuilder._add_member(
                    archive,
                    "credential.jwt",
                    credential_bytes,
                )

        gzip_buffer = io.BytesIO()
        with gzip.GzipFile(
            fileobj=gzip_buffer,
            mode="wb",
            mtime=0,
            compresslevel=6,
        ) as compressed:
            compressed.write(tar_buffer.getvalue())
        return gzip_buffer.getvalue()

    @staticmethod
    def _add_member(archive: tarfile.TarFile, name: str, data: bytes) -> None:
        """Add one regular file with inert, normalized tar metadata."""

        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = 0o644
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        archive.addfile(info, io.BytesIO(data))


__all__ = ["BundleNotAvailableError", "EvidenceBundleBuilder"]
